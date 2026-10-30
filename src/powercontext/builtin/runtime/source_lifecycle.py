# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Source lifecycle state over the native Memory and Topic Memory families.

One Scope registers its evidence graph explicitly: Sources are the roots, and
every Memory or Topic Memory node names the parents that support it. A Source
withdrawal then propagates through that graph, so unsupported content leaves the
native search and Context paths while every registered body stays exactly
readable for audit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory.errors import MemoryEntryNotFoundError
from powercontext.builtin.persistence.codec import dump_model, load_model, stored_bytes
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.errors import RepositoryNotFoundError
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_EVENTS_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
)
from powercontext.builtin.records import BaseValueNotFoundError
from powercontext.builtin.runtime.models import GetMemoryEntryRequest, RetireMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import ArtifactNotFoundError
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

SourceLifecycleKind = Literal["source", "memory", "topic"]
SourceLifecyclePolicy = Literal["all", "any"]
SourceLifecyclePurpose = Literal["generation", "audit"]

_POLICIES: frozenset[str] = frozenset({"all", "any"})
_PURPOSES: frozenset[str] = frozenset({"generation", "audit"})
_MAX_REASON_LENGTH = 512


class SourceLifecycleError(ValueError):
    """Report a rejected Source lifecycle operation."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        super().__init__(code if detail is None else f"{code}: {detail}")


class SourceLifecycleEvent(BaseModel):
    """One Source availability transition applied to one lifecycle version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    reason: str
    revision: int = Field(ge=1)
    sequence: int = Field(ge=1)
    available: bool

    @field_validator("node_id", "reason")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")  # noqa: TRY003
        return value


class _SourceReference(RootModel[SourceRef]):
    root: SourceRef


class _MemoryReference(RootModel[MemoryCitation]):
    root: MemoryCitation


class _TopicReference(RootModel[ArtifactRef]):
    root: ArtifactRef


class _ParentIds(RootModel[tuple[str, ...]]):
    root: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LifecycleNode:
    """One registered node of one Scope's evidence graph."""

    node_id: str
    kind: SourceLifecycleKind
    parents: tuple[str, ...]
    policy: SourceLifecyclePolicy | None
    revision: int
    sequence: int
    available: bool
    reference: SourceRef | MemoryCitation | ArtifactRef


@dataclass(frozen=True, slots=True)
class _StoredEvent:
    """One applied lifecycle event, kept for replay detection."""

    revision: int
    available: bool
    reason: str


class _LifecycleStore:
    """Read and write the durable evidence graph of one Scope."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def nodes(self, scope_id: str) -> dict[str, _LifecycleNode]:
        async with self._database.transaction() as connection:
            return await self._read_nodes(connection, scope_id)

    async def node(self, scope_id: str, node_id: str) -> _LifecycleNode | None:
        async with self._database.transaction() as connection:
            row = await self._read_node(connection, scope_id, node_id)
        return None if row is None else _node(row)

    async def insert_node(self, scope_id: str, node: _LifecycleNode) -> None:
        async with self._database.transaction() as connection:
            await connection.execute(
                insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                    scope_id=scope_id,
                    node_id=node.node_id,
                    kind=node.kind,
                    policy=node.policy,
                    parents=_dump_parents(node.parents),
                    reference=_dump_reference(node),
                    revision=node.revision,
                    sequence=node.sequence,
                    available=node.available,
                )
            )

    async def event(self, scope_id: str, node_id: str, sequence: int) -> _StoredEvent | None:
        async with self._database.transaction() as connection:
            row = (
                (
                    await connection.execute(
                        select(SOURCE_LIFECYCLE_EVENTS_TABLE).where(
                            SOURCE_LIFECYCLE_EVENTS_TABLE.c.scope_id == scope_id,
                            SOURCE_LIFECYCLE_EVENTS_TABLE.c.node_id == node_id,
                            SOURCE_LIFECYCLE_EVENTS_TABLE.c.sequence == sequence,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return _StoredEvent(
            revision=int(row["revision"]),
            available=bool(row["available"]),
            reason=str(row["reason"]),
        )

    async def record_event(self, scope_id: str, event: SourceLifecycleEvent) -> None:
        """Record one applied event and its Source state together."""

        async with self._database.transaction() as connection:
            await connection.execute(
                insert(SOURCE_LIFECYCLE_EVENTS_TABLE).values(
                    scope_id=scope_id,
                    node_id=event.node_id,
                    sequence=event.sequence,
                    revision=event.revision,
                    available=event.available,
                    reason=event.reason,
                )
            )
            await connection.execute(
                update(SOURCE_LIFECYCLE_NODES_TABLE)
                .where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == event.node_id,
                )
                .values(available=event.available, sequence=event.sequence)
            )

    async def set_source_state(
        self,
        scope_id: str,
        node_id: str,
        *,
        available: bool,
        sequence: int,
    ) -> None:
        async with self._database.transaction() as connection:
            await connection.execute(
                update(SOURCE_LIFECYCLE_NODES_TABLE)
                .where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
                )
                .values(available=available, sequence=sequence)
            )

    async def set_availability(
        self,
        scope_id: str,
        availability: Mapping[str, bool],
        previous: Mapping[str, _LifecycleNode],
    ) -> None:
        changed = [(node_id, value) for node_id, value in availability.items() if previous[node_id].available != value]
        if not changed:
            return
        async with self._database.transaction() as connection:
            for node_id, value in changed:
                await connection.execute(
                    update(SOURCE_LIFECYCLE_NODES_TABLE)
                    .where(
                        SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                        SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
                    )
                    .values(available=value)
                )

    async def _read_nodes(self, connection: AsyncConnection, scope_id: str) -> dict[str, _LifecycleNode]:
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                )
            )
        ).mappings()
        return {str(row["node_id"]): _node(row) for row in rows}

    async def _read_node(self, connection: AsyncConnection, scope_id: str, node_id: str):
        return (
            (
                await connection.execute(
                    select(SOURCE_LIFECYCLE_NODES_TABLE).where(
                        SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                        SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )


class SourceLifecycleApplication:
    """Select the scoped Source lifecycle application service."""

    def __init__(
        self,
        runtime: BuiltinRuntime,
        *,
        database: AsyncDatabase | None = None,
        sources: SourceRepository | None = None,
    ) -> None:
        self._runtime = runtime
        self._database = database
        self._sources = sources

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleService:
        return ScopedSourceLifecycleService(self._runtime, self._database, self._sources, scope_id)

    async def withdrawn_topics(self, scope_id: str, /) -> frozenset[str]:
        """Return the Topic Artifacts whose registered support is currently withdrawn."""

        if self._database is None:
            return frozenset()
        nodes = await _LifecycleStore(self._database).nodes(validate_scope_id(scope_id))
        return frozenset(
            node.reference.artifact_id
            for node in nodes.values()
            if node.kind == "topic" and not node.available and isinstance(node.reference, ArtifactRef)
        )


class ScopedSourceLifecycleService:
    """Register evidence nodes and apply Source availability transitions in one Scope."""

    def __init__(
        self,
        runtime: BuiltinRuntime,
        database: AsyncDatabase | None,
        sources: SourceRepository | None,
        scope_id: str,
    ) -> None:
        self._runtime = runtime
        self._database = database
        self._sources = sources
        self.scope_id = validate_scope_id(scope_id)
        self._store = None if database is None else _LifecycleStore(database)

    async def register_source(self, node_id: str, source_ref: SourceRef, revision: int) -> None:
        """Register one captured Source as a lifecycle root."""

        key = _require_node_id(node_id)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise SourceLifecycleError("invalid-revision")
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("invalid-source-reference")
        await self._require_source(source_ref)
        await self._register(
            _LifecycleNode(
                node_id=key,
                kind="source",
                parents=(),
                policy=None,
                revision=revision,
                sequence=0,
                available=True,
                reference=source_ref,
            )
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Register one native Memory entry and the parents that support it."""

        key = _require_node_id(node_id)
        resolved_policy = _require_policy(policy)
        parent_ids = _require_parents(node_id, parents)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("invalid-memory-reference")
        await self._require_citation(citation)
        nodes = await self._nodes()
        _require_known_parents(nodes, parent_ids)
        available = _support(nodes, parent_ids, resolved_policy)
        stored = await self._register(
            _LifecycleNode(
                node_id=key,
                kind="memory",
                parents=parent_ids,
                policy=resolved_policy,
                revision=0,
                sequence=0,
                available=available,
                reference=citation,
            )
        )
        await self._apply_memory_state({stored.node_id: stored.available})

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Register one Topic Memory Artifact and the parents that support it."""

        key = _require_node_id(node_id)
        resolved_policy = _require_policy(policy)
        parent_ids = _require_parents(node_id, parents)
        if not isinstance(artifact_ref, ArtifactRef):
            raise SourceLifecycleError("invalid-artifact-reference")
        await self._require_artifact(artifact_ref)
        nodes = await self._nodes()
        _require_known_parents(nodes, parent_ids)
        available = _support(nodes, parent_ids, resolved_policy)
        await self._register(
            _LifecycleNode(
                node_id=key,
                kind="topic",
                parents=parent_ids,
                policy=resolved_policy,
                revision=0,
                sequence=0,
                available=available,
                reference=artifact_ref,
            )
        )

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        """Apply one Source availability event and return every available node."""

        store = self._store
        if store is None:
            raise SourceLifecycleError("lifecycle-unavailable")
        nodes = await store.nodes(self.scope_id)
        source = nodes.get(event.node_id)
        if source is None or source.kind != "source":
            raise SourceLifecycleError("unknown-source")
        if event.revision != source.revision:
            raise SourceLifecycleError("revision-mismatch")
        if event.sequence < source.sequence:
            raise SourceLifecycleError("sequence-order")
        stored = await store.event(self.scope_id, event.node_id, event.sequence)
        if stored is not None:
            if not _same_event(stored, event):
                raise SourceLifecycleError("sequence-conflict")
        elif event.sequence == source.sequence:
            raise SourceLifecycleError("sequence-conflict")
        else:
            await store.record_event(self.scope_id, event)
        # The event is the authority for its own Source, so an interrupted write
        # leaves the recorded availability to be replayed into the node state.
        await store.set_source_state(
            self.scope_id,
            event.node_id,
            available=event.available,
            sequence=event.sequence,
        )
        nodes = await store.nodes(self.scope_id)
        availability = _propagate(nodes)
        await store.set_availability(self.scope_id, availability, nodes)
        await self._apply_memory_state(availability, reason=event.reason)
        return sorted(node_id for node_id, available in availability.items() if available)

    async def resolve(self, node_id: str, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        """Return one node's registered reference for the requested use."""

        if purpose not in _PURPOSES:
            raise SourceLifecycleError("unknown-purpose")
        key = _require_node_id(node_id)
        store = self._store
        if store is None:
            raise SourceLifecycleError("lifecycle-unavailable")
        node = await store.node(self.scope_id, key)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("unavailable-node")
        return node.reference

    async def _register(self, node: _LifecycleNode) -> _LifecycleNode:
        """Insert one node, or accept an identical registration as a no-op."""

        store = self._store
        if store is None:
            raise SourceLifecycleError("lifecycle-unavailable")
        existing = await store.node(self.scope_id, node.node_id)
        if existing is not None:
            if not _same_binding(existing, node):
                raise SourceLifecycleError("conflicting-registration")
            return existing
        await store.insert_node(self.scope_id, node)
        return node

    async def _nodes(self) -> dict[str, _LifecycleNode]:
        store = self._store
        if store is None:
            raise SourceLifecycleError("lifecycle-unavailable")
        return await store.nodes(self.scope_id)

    async def _require_source(self, source_ref: SourceRef) -> None:
        """Reject a Source that this Scope cannot read."""

        if self._sources is None or self._database is None:
            raise SourceLifecycleError("lifecycle-unavailable")
        try:
            async with self._database.transaction() as connection:
                await self._sources.get(connection, self.scope_id, source_ref)
        except RepositoryNotFoundError:
            raise SourceLifecycleError("unknown-source") from None

    async def _require_citation(self, citation: MemoryCitation) -> None:
        """Reject a Memory citation that this Scope cannot read."""

        try:
            await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=citation))
        except (ArtifactNotFoundError, MemoryEntryNotFoundError):
            raise SourceLifecycleError("unknown-memory-entry") from None

    async def _require_artifact(self, artifact_ref: ArtifactRef) -> None:
        """Reject an Artifact revision that this Scope cannot read."""

        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                artifact_ref.family,
                artifact_ref.artifact_id,
                artifact_ref.revision,
            )
        except (ArtifactNotFoundError, BaseValueNotFoundError):
            raise SourceLifecycleError("unknown-artifact") from None

    async def _apply_memory_state(self, availability: Mapping[str, bool], *, reason: str | None = None) -> None:
        """Align the native Memory entry states with the computed availability.

        The native state is the comparison target, so an interrupted propagation
        resumes where it stopped instead of trusting the recorded availability.
        """

        store = self._store
        if store is None:
            raise SourceLifecycleError("lifecycle-unavailable")
        nodes = await store.nodes(self.scope_id)
        targets = [node for node in nodes.values() if node.kind == "memory" and node.node_id in availability]
        if not targets:
            return
        audit_reason = _state_reason(reason)
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            artifact_id = context.artifacts.memory_artifact_id
            for node in targets:
                desired = bool(availability[node.node_id])
                entry_id = _memory_entry_id(node)
                head = await _head_or_none(service, artifact_id)
                if head is None:
                    return
                entry = next(
                    (item for item in head.content.manifest.entries if item.entry_id == entry_id),
                    None,
                )
                if entry is None or entry.state == ("active" if desired else "inactive"):
                    continue
                version = next(
                    (item for item in await service.entries(head) if item.entry_id == entry_id),
                    None,
                )
                if version is None:
                    continue
                if desired:
                    await service.reactivate(head, entries=(version,), reason=audit_reason)
                    continue
                citation = MemoryCitation(
                    memory_ref=head.as_ref(),
                    entry_id=version.entry_id,
                    entry_version_id=version.entry_version_id,
                )
                await self._runtime.memory.for_scope(self.scope_id).retire(
                    RetireMemoryEntryRequest(citation=citation, reason=audit_reason)
                )


def _node(row) -> _LifecycleNode:
    kind = str(row["kind"])
    return _LifecycleNode(
        node_id=str(row["node_id"]),
        kind=kind,
        parents=_load_parents(row["parents"]),
        policy=None if row["policy"] is None else str(row["policy"]),
        revision=int(row["revision"]),
        sequence=int(row["sequence"]),
        available=bool(row["available"]),
        reference=_load_reference(kind, row["reference"]),
    )


def _dump_parents(parents: tuple[str, ...]) -> bytes:
    return dump_model(_ParentIds(parents), kind="source-lifecycle", name="parents")


def _load_parents(payload: object) -> tuple[str, ...]:
    return load_model(
        _ParentIds,
        stored_bytes(payload, column="parents"),
        kind="source-lifecycle",
        name="parents",
    ).root


def _dump_reference(node: _LifecycleNode) -> bytes:
    if node.kind == "source":
        return dump_model(_SourceReference(node.reference), kind="source-lifecycle", name="source-reference")
    if node.kind == "memory":
        return dump_model(_MemoryReference(node.reference), kind="source-lifecycle", name="memory-reference")
    return dump_model(_TopicReference(node.reference), kind="source-lifecycle", name="topic-reference")


def _load_reference(kind: str, payload: object) -> SourceRef | MemoryCitation | ArtifactRef:
    data = stored_bytes(payload, column="reference")
    if kind == "source":
        return load_model(
            _SourceReference,
            data,
            kind="source-lifecycle",
            name="source-reference",
        ).root
    if kind == "memory":
        return load_model(
            _MemoryReference,
            data,
            kind="source-lifecycle",
            name="memory-reference",
        ).root
    return load_model(
        _TopicReference,
        data,
        kind="source-lifecycle",
        name="topic-reference",
    ).root


def _same_binding(existing: _LifecycleNode, requested: _LifecycleNode) -> bool:
    """Return whether two registrations describe the same binding."""

    return (
        existing.kind == requested.kind
        and existing.parents == requested.parents
        and existing.policy == requested.policy
        and existing.revision == requested.revision
        and existing.reference == requested.reference
    )


def _same_event(stored: _StoredEvent, event: SourceLifecycleEvent) -> bool:
    """Return whether a replayed event carries the content already applied."""

    return stored.revision == event.revision and stored.available == event.available and stored.reason == event.reason


def _require_node_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not node_id.strip() or len(node_id) > MAX_SOURCE_ID_LENGTH:
        raise SourceLifecycleError("invalid-node-id")
    return node_id


def _require_policy(policy: str) -> SourceLifecyclePolicy:
    if not isinstance(policy, str) or policy not in _POLICIES:
        raise SourceLifecycleError("invalid-policy")
    return policy  # type: ignore[return-value]


def _require_parents(node_id: str, parents: Sequence[str]) -> tuple[str, ...]:
    """Validate one derived node's parent list before it is registered."""

    if isinstance(parents, str) or not isinstance(parents, Sequence):
        raise SourceLifecycleError("invalid-parents")
    resolved = tuple(parents)
    if not resolved:
        raise SourceLifecycleError("missing-parents")
    if len(set(resolved)) != len(resolved):
        raise SourceLifecycleError("duplicate-parents")
    for parent in resolved:
        _require_node_id(parent)
    if node_id in resolved:
        raise SourceLifecycleError("self-dependency")
    return tuple(sorted(resolved))


def _require_known_parents(nodes: Mapping[str, _LifecycleNode], parents: Sequence[str]) -> None:
    for parent in parents:
        if parent not in nodes:
            raise SourceLifecycleError("unknown-parent")


def _support(nodes: Mapping[str, _LifecycleNode], parents: Sequence[str], policy: str) -> bool:
    """Return whether the registered parents still support one derived node."""

    support = [nodes[parent].available for parent in parents]
    return all(support) if policy == "all" else any(support)


def _propagate(nodes: Mapping[str, _LifecycleNode]) -> dict[str, bool]:
    """Compute every node's availability from its parents' support."""

    availability = {node_id: node.available for node_id, node in nodes.items()}
    for node_id in _topological_order(nodes):
        node = nodes[node_id]
        if node.kind == "source":
            continue
        support = [availability[parent] for parent in node.parents]
        availability[node_id] = all(support) if node.policy == "all" else any(support)
    return availability


def _topological_order(nodes: Mapping[str, _LifecycleNode]) -> list[str]:
    """Order the graph so every parent is computed before its dependants."""

    order: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()
    for node_id in sorted(nodes):
        _visit(node_id, nodes, visiting, visited, order)
    return order


def _visit(
    node_id: str,
    nodes: Mapping[str, _LifecycleNode],
    visiting: set[str],
    visited: set[str],
    order: list[str],
) -> None:
    if node_id in visited:
        return
    if node_id in visiting:
        raise SourceLifecycleError("cyclic-dependency")
    visiting.add(node_id)
    node = nodes.get(node_id)
    if node is not None:
        for parent in node.parents:
            _visit(parent, nodes, visiting, visited, order)
    visiting.discard(node_id)
    visited.add(node_id)
    order.append(node_id)


def _memory_entry_id(node: _LifecycleNode) -> str:
    """Return the native Memory entry one registered Memory node points at."""

    if not isinstance(node.reference, MemoryCitation):
        raise SourceLifecycleError("invalid-memory-reference")
    return node.reference.entry_id


def _state_reason(reason: str | None) -> str:
    """Describe one lifecycle transition in the native Memory audit trail."""

    detail = "registered without support" if reason is None else reason.strip()
    return f"source-lifecycle: {detail}"[:_MAX_REASON_LENGTH]


async def _head_or_none(service, artifact_id: str):
    """Return the current Memory head, or ``None`` when the Scope has none."""

    try:
        return await service.head(artifact_id)
    except ArtifactNotFoundError:
        return None


__all__ = [
    "ScopedSourceLifecycleService",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "SourceLifecycleKind",
    "SourceLifecyclePolicy",
    "SourceLifecyclePurpose",
]
