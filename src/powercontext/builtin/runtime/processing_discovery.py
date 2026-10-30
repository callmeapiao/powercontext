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

"""Bounded reconciliation and count admission of Source-driven Family progress."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.supervision import ArtifactProcessingFence, ArtifactProcessingLeaseRepository
from powercontext.builtin.persistence.tables import (
    ARTIFACT_PROCESSING_INTENTS_TABLE,
    PROFILE_POLICIES_TABLE,
    SOURCE_JOURNAL_HEADS_TABLE,
)


async def enabled_profile_scopes(connection: AsyncConnection, scope_ids: tuple[str, ...]) -> frozenset[str]:
    """Qualify one bounded automatic page before accepting any invocation.

    Hold enabled Policy rows through admission so concurrent policy changes
    serialize with the decision. This reads metadata only, before any Worker
    authorization, model construction, or retry state exists for the page.
    """

    if not scope_ids:
        return frozenset()
    table = PROFILE_POLICIES_TABLE
    rows = await connection.scalars(
        select(table.c.scope_id)
        .where(table.c.scope_id.in_(scope_ids), table.c.generation_enabled.is_(True))
        .with_for_update()
    )
    return frozenset(rows)


class SourceProcessingPendingProvider:
    """Inspect journal metadata, never Source bodies, prompts, or models."""

    def __init__(self, database: AsyncDatabase, binding_name: str, artifact_family: str) -> None:
        self._database = database
        self._binding = binding_name
        self._family = artifact_family

    async def reconcile(self, after_scope_id: str | None, limit: int, *, fence: ArtifactProcessingFence) -> str | None:
        heads = SOURCE_JOURNAL_HEADS_TABLE
        statement = select(heads.c.scope_id, heads.c.position).order_by(heads.c.scope_id).limit(limit)
        if after_scope_id is not None:
            statement = statement.where(heads.c.scope_id > after_scope_id)
        if self._family == "profile":
            statement = statement.join(PROFILE_POLICIES_TABLE, PROFILE_POLICIES_TABLE.c.scope_id == heads.c.scope_id)
            statement = statement.where(PROFILE_POLICIES_TABLE.c.generation_enabled.is_(True))
        async with self._database.transaction() as connection:
            await ArtifactProcessingLeaseRepository().require_fence(connection, fence)
            rows = (await connection.execute(statement)).all()
            intents = ArtifactProcessingIntentRepository()
            cursors = SourceCursorRepository()
            for scope, high in rows:
                cursor = await cursors.load(connection, scope, self._binding)
                if int(high) <= (0 if cursor is None else cursor.cursor.sequence):
                    continue
                intent = await intents.load(connection, scope, self._binding, for_update=True)
                if intent is None or intent.dirty_generation == intent.clean_generation:
                    await intents.mark_dirty(connection, scope, self._binding)
        return str(rows[-1][0]) if len(rows) == limit else None


@dataclass(frozen=True, slots=True)
class SourceCountPage:
    """One bounded page of scopes and the keyset position that follows it."""

    ready: tuple[str, ...]
    next_after: str | None


async def count_ready_scopes(
    connection: AsyncConnection,
    *,
    binding_name: str,
    threshold: int,
    limit: int,
    after_scope_id: str | None = None,
) -> SourceCountPage:
    """Report scopes whose pending Source count reached one threshold.

    The count is the number of persisted journal identities after the
    Family's last successful Source cursor, so a replayed capture or a
    receipt trust upgrade never adds a pending identity. A lineage-only
    server Source still owns a journal position and therefore still counts.
    """

    if threshold < 1 or limit < 1:
        raise ValueError("Source count threshold and page size must be positive")  # noqa: TRY003
    intents = ARTIFACT_PROCESSING_INTENTS_TABLE
    statement = select(intents.c.scope_id).where(intents.c.binding_name == binding_name)
    if after_scope_id is not None:
        statement = statement.where(intents.c.scope_id > after_scope_id)
    statement = statement.order_by(intents.c.scope_id).limit(limit)
    scope_ids = tuple(str(row[0]) for row in (await connection.execute(statement)).all())
    if not scope_ids:
        return SourceCountPage(ready=(), next_after=None)
    heads = SOURCE_JOURNAL_HEADS_TABLE
    positions = {
        str(row[0]): int(row[1])
        for row in (
            await connection.execute(select(heads.c.scope_id, heads.c.position).where(heads.c.scope_id.in_(scope_ids)))
        ).all()
    }
    cursors = await SourceCursorRepository().load_many(connection, scope_ids, binding_name)
    ready = []
    for scope_id in scope_ids:
        stored = cursors.get(scope_id)
        sequence = 0 if stored is None else stored.cursor.sequence
        if positions.get(scope_id, 0) - sequence >= threshold:
            ready.append(scope_id)
    return SourceCountPage(ready=tuple(ready), next_after=scope_ids[-1] if len(scope_ids) == limit else None)
