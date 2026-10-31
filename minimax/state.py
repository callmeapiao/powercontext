"""Task state persisted through PowerContext.

The development context of a task has to survive a process restart, otherwise a
new session has to rediscover what the previous one already did.  This module
stores that state with PowerContext's own artifact lifecycle:

* every imported engineering material becomes a **Source** (spec / code / test),
* every saved task state becomes a new **revision** of one artifact, so the
  history of a task is the artifact's revision history,
* the artifact's **lineage** cites the sources it was derived from, which makes
  the provenance of a task state inspectable.

The concrete stores below implement PowerContext's public protocols
(``ArtifactCatalog``, ``ArtifactStore``, ``SourceCatalogBackend``,
``SourceStore``) on top of SQLite, and are composed through PowerContext's
public ``PowerContext`` / ``Sources`` / ``Artifacts`` objects.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field
from powercontext import Artifacts, PowerContext, Sources
from powercontext.artifacts import Artifact, ArtifactDraft, ArtifactLineage
from powercontext.sources import Source, SourceCatalog, SourceMaterialization
from powercontext.sources.protocols import SourceCatalogBackend

from .config import Settings


# --------------------------------------------------------------------- models
class ValidationRecord(BaseModel):
    """One test run recorded against a task state."""

    command: str
    exit_code: int
    passed: int = 0
    failed: int = 0
    summary: str = ""
    recorded_at: str = ""


class TaskState(BaseModel):
    """Everything a later session needs to continue a task."""

    task_id: str
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    active_specs: list[str] = Field(default_factory=list)
    superseded_specs: list[str] = Field(default_factory=list)
    target_symbols: list[str] = Field(default_factory=list)
    code_files: list[str] = Field(default_factory=list)
    test_paths: list[str] = Field(default_factory=list)
    done: list[str] = Field(default_factory=list)
    todo: list[str] = Field(default_factory=list)
    validations: list[ValidationRecord] = Field(default_factory=list)
    material_sources: list[str] = Field(default_factory=list)
    updated_at: str = ""

    def to_json(self) -> str:
        return self.model_dump_json()


class MaterialSource(Source):
    """One imported engineering material registered as a PowerContext Source."""

    kind: str = "material"
    path: str = ""
    payload: str = ""
    digest: str = ""


class TaskStateArtifact(Artifact[TaskState]):
    """An artifact revision holding one task state."""

    family: ClassVar[str] = "task-state"


class TaskStateDraft(ArtifactDraft[TaskState]):
    """The content and evidence supplied when writing a task state."""

    family: ClassVar[str] = "task-state"


# ------------------------------------------------------------------- adapters
class MaterialSourceAdapter:
    """Adapter that resolves and reads :class:`MaterialSource` values."""

    input_class = MaterialSource
    source_class = MaterialSource
    name = "material"

    async def resolve(self, value: MaterialSource, /) -> MaterialSource:
        return value

    async def read(self, source: MaterialSource, /) -> str:
        return source.payload


# --------------------------------------------------------------------- stores
SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    source_type TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    definition  TEXT NOT NULL,
    PRIMARY KEY (source_type, source_id)
);
CREATE TABLE IF NOT EXISTS artifact (
    family      TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    content     TEXT NOT NULL,
    lineage     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (family, artifact_id, revision)
);
"""


class SqliteArtifactStore:
    """PowerContext ``ArtifactStore`` backed by SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def add(self, draft: TaskStateDraft, /) -> TaskStateArtifact:
        """Commit the first revision of an artifact."""

        row = self._conn.execute(
            "SELECT MAX(revision) FROM artifact WHERE family = ? AND artifact_id = ?",
            (draft.family, draft.content.task_id),
        ).fetchone()
        revision = 1 if row[0] is None else int(row[0]) + 1
        return self._write(draft, revision)

    async def revise(self, artifact: TaskStateArtifact, draft: TaskStateDraft, /) -> TaskStateArtifact:
        """Commit a new revision only if ``artifact`` is still the latest one."""

        row = self._conn.execute(
            "SELECT MAX(revision) FROM artifact WHERE family = ? AND artifact_id = ?",
            (draft.family, artifact.artifact_id),
        ).fetchone()
        latest = 1 if row[0] is None else int(row[0])
        if latest != artifact.revision:
            from powercontext import RevisionConflictError

            raise RevisionConflictError(artifact, latest)
        return self._write(draft, latest + 1)

    def _write(self, draft: TaskStateDraft, revision: int) -> TaskStateArtifact:
        lineage = ArtifactLineage(sources=draft.sources, artifacts=draft.artifacts)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT OR REPLACE INTO artifact"
            " (family, artifact_id, revision, content, lineage, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                draft.family,
                draft.content.task_id,
                revision,
                draft.content.model_dump_json(),
                lineage.model_dump_json(),
                created_at,
            ),
        )
        self._conn.commit()
        return TaskStateArtifact(
            artifact_id=draft.content.task_id,
            revision=revision,
            content=draft.content,
            lineage=lineage,
        )


class SqliteArtifactCatalog:
    """PowerContext ``ArtifactCatalog`` backed by SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def get(self, artifact: TaskStateArtifact, /) -> TaskStateArtifact:
        row = self._conn.execute(
            "SELECT content, lineage FROM artifact WHERE family = ? AND artifact_id = ? AND revision = ?",
            (artifact.family, artifact.artifact_id, artifact.revision),
        ).fetchone()
        if row is None:
            from powercontext import ArtifactNotFoundError

            raise ArtifactNotFoundError(artifact)
        return self._build(artifact.family, artifact.artifact_id, artifact.revision, row)

    async def latest(self, artifact: TaskStateArtifact, /) -> TaskStateArtifact:
        row = self._conn.execute(
            "SELECT MAX(revision) FROM artifact WHERE family = ? AND artifact_id = ?",
            (artifact.family, artifact.artifact_id),
        ).fetchone()
        if row[0] is None:
            from powercontext import ArtifactNotFoundError

            raise ArtifactNotFoundError(artifact)
        return await self.get(
            TaskStateArtifact(artifact_id=artifact.artifact_id, revision=int(row[0]), content=artifact.content)
        )

    async def revisions(self, artifact: TaskStateArtifact, /) -> tuple[TaskStateArtifact, ...]:
        rows = self._conn.execute(
            "SELECT revision, content, lineage FROM artifact WHERE family = ? AND artifact_id = ?"
            " ORDER BY revision ASC",
            (artifact.family, artifact.artifact_id),
        ).fetchall()
        if not rows:
            from powercontext import ArtifactNotFoundError

            raise ArtifactNotFoundError(artifact)
        return tuple(self._build(artifact.family, artifact.artifact_id, int(rev), rest) for rev, *rest in rows)

    def _build(self, family: str, artifact_id: str, revision: int, row: tuple) -> TaskStateArtifact:
        content, lineage = row
        return TaskStateArtifact(
            artifact_id=artifact_id,
            revision=revision,
            content=TaskState.model_validate_json(content),
            lineage=ArtifactLineage.model_validate(json.loads(lineage)),
        )


class SqliteSourceStore:
    """PowerContext ``SourceStore`` backed by SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def add(self, value: MaterialSource, /) -> MaterialSource:
        self._conn.execute(
            "INSERT OR REPLACE INTO source (source_type, source_id, payload, definition) VALUES (?,?,?,?)",
            ("material", value.name, value.payload, value.model_dump_json()),
        )
        self._conn.commit()
        return value


class SqliteSourceCatalogBackend:
    """PowerContext ``SourceCatalogBackend`` backed by SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def get(self, source: MaterialSource, /) -> MaterialSource:
        row = self._conn.execute(
            "SELECT definition FROM source WHERE source_type = 'material' AND source_id = ?",
            (source.name,),
        ).fetchone()
        if row is None:
            from powercontext import SourceNotFoundError

            raise SourceNotFoundError(source)
        return MaterialSource.model_validate_json(row[0])

    async def list(self) -> tuple[MaterialSource, ...]:
        rows = self._conn.execute(
            "SELECT definition FROM source WHERE source_type = 'material' ORDER BY source_id"
        ).fetchall()
        return tuple(MaterialSource.model_validate_json(row[0]) for row in rows)


# -------------------------------------------------------------------- facade
class TaskStateStore:
    """Facade over the PowerContext composition root."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn = sqlite3.connect(str(settings.state_db), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        catalog = SqliteArtifactCatalog(self._conn)
        store = SqliteArtifactStore(self._conn)
        self._artifacts = Artifacts(catalog=catalog, store=store)
        source_catalog = SourceCatalog(
            backend=SqliteSourceCatalogBackend(self._conn),
            adapters=[MaterialSourceAdapter()],
        )
        self._sources = Sources(catalog=source_catalog, store=SqliteSourceStore(self._conn))
        self._powercontext = PowerContext(sources=self._sources, artifacts=self._artifacts, triggers=())

    @property
    def powercontext(self) -> PowerContext:
        """The composed PowerContext instance."""

        return self._powercontext

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------- materials
    async def register_material(self, *, name: str, kind: str, path: str, payload: str, digest: str) -> MaterialSource:
        """Register one imported material as a PowerContext Source."""

        source = MaterialSource(
            name=name,
            materialization=SourceMaterialization.CAPTURED,
            description=f"{kind} material imported from {path}",
            kind=kind,
            path=path,
            payload=payload,
            digest=digest,
        )
        return await self._sources.add(source)

    async def list_materials(self) -> tuple[MaterialSource, ...]:
        """Return every registered material."""

        return await self._sources.list()

    # ----------------------------------------------------------------- state
    async def save(self, state: TaskState, material_sources: list[str] | None = None) -> TaskStateArtifact:
        """Persist a new revision of the task state."""

        state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        refs = []
        for name in material_sources or state.material_sources:
            refs.append(_source_ref(name))
        draft = TaskStateDraft(content=state, sources=tuple(refs))
        try:
            existing = await self._artifacts.latest(
                TaskStateArtifact(artifact_id=state.task_id, revision=1, content=state)
            )
        except Exception:
            return await self._artifacts.add(draft)
        return await self._artifacts.revise(existing, draft)

    async def load(self, task_id: str) -> TaskStateArtifact | None:
        """Return the latest revision of a task state, or ``None``."""

        try:
            return await self._artifacts.latest(
                TaskStateArtifact(artifact_id=task_id, revision=1, content=TaskState(task_id=task_id))
            )
        except Exception:
            return None

    async def history(self, task_id: str) -> tuple[TaskStateArtifact, ...]:
        """Return every revision of a task state."""

        try:
            return await self._artifacts.revisions(
                TaskStateArtifact(artifact_id=task_id, revision=1, content=TaskState(task_id=task_id))
            )
        except Exception:
            return ()

    def source_ref_for(self, name: str) -> Any:
        """Return the PowerContext reference of a registered material."""

        return _source_ref(name)


def _source_ref(name: str) -> Any:
    from powercontext.sources import SourceRef

    return SourceRef(source_type="material", source_id=name)
