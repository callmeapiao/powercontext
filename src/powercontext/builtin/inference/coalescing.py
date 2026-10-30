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

"""Cross-call embedding request coalescing and bounded vector caching.

Concurrent callers often embed the same texts, and repeated writes re-embed
texts whose vectors are already known. This module wraps one embedding model
with a shared request pool: texts requested inside one coalescing window are
merged into deduplicated provider batches, late callers join batches that are
already running, and successful vectors are reused from a bounded LRU cache.
Every caller keeps its own ordered result and its own usage accounting, and a
cancelled or failed caller never disturbs the other waiters of its batch.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
    from powercontext.builtin.inference.protocols import EmbeddingModel

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024

_EmbeddingCall = Callable[[tuple[str, ...]], Awaitable[EmbeddingResult]]


@dataclass(frozen=True, slots=True, eq=False)
class _BatchOutcome:
    """Provider usage of one underlying batch, shared by every waiter of that batch."""

    usage: InferenceUsage


@dataclass(frozen=True, slots=True)
class _TextOutcome:
    """One resolved text vector and the batch that produced it."""

    vector: EmbeddingVector
    batch: _BatchOutcome


@dataclass(slots=True)
class _PendingText:
    """One text waiting for the batch that will resolve it."""

    text: str
    future: asyncio.Future[_TextOutcome]


def _discard_unretrieved_exception(future: asyncio.Future[_TextOutcome]) -> None:
    """Mark a settled failure as retrieved so abandoned waiters keep the loop quiet."""

    if not future.cancelled():
        future.exception()


def _merge_usage(outcomes: Iterable[_BatchOutcome]) -> InferenceUsage:
    """Sum provider usage over the distinct batches one caller waited for."""

    requests = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    for outcome in outcomes:
        usage = outcome.usage
        requests += usage.requests
        if usage.input_tokens is not None:
            input_tokens = (input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            output_tokens = (output_tokens or 0) + usage.output_tokens
    return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)


class _CoalescingPool:
    """Merge concurrent embedding requests and cache successful vectors for one base call."""

    def __init__(
        self,
        call: _EmbeddingCall,
        *,
        max_batch_size: int,
        coalesce_seconds: float,
        cache_size: int,
    ) -> None:
        self._call = call
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_seconds
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._queued: dict[str, _PendingText] = {}
        self._inflight: dict[str, _PendingText] = {}
        self._flush_task: asyncio.Task[None] | None = None

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed an ordered text batch, sharing work with every concurrent caller."""

        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        cached: dict[str, EmbeddingVector] = {}
        pending: dict[str, _PendingText] = {}
        for text in texts:
            if text in cached or text in pending:
                continue
            vector = self._cached_vector(text)
            if vector is not None:
                cached[text] = vector
                continue
            entry = self._inflight.get(text) or self._queued.get(text)
            if entry is None:
                entry = self._register(text)
            pending[text] = entry

        outcomes: dict[str, _TextOutcome] = {}
        if pending:
            outcomes = await self._resolve(pending)

        vectors: list[EmbeddingVector] = []
        batches: dict[int, _BatchOutcome] = {}
        for text in texts:
            if text in cached:
                vectors.append(cached[text])
                continue
            outcome = outcomes[text]
            vectors.append(outcome.vector)
            batches[id(outcome.batch)] = outcome.batch
        return EmbeddingResult(vectors=tuple(vectors), usage=_merge_usage(batches.values()))

    async def _resolve(self, pending: Mapping[str, _PendingText]) -> dict[str, _TextOutcome]:
        """Wait for every pending text without cancelling shared work when one waiter stops."""

        await asyncio.wait([entry.future for entry in pending.values()])
        outcomes: dict[str, _TextOutcome] = {}
        failure: BaseException | None = None
        for entry in pending.values():
            future = entry.future
            if future.cancelled():
                if failure is None:
                    failure = asyncio.CancelledError()
                continue
            exception = future.exception()
            if exception is not None:
                if failure is None:
                    failure = exception
                continue
            outcomes[entry.text] = future.result()
        if failure is not None:
            raise failure
        return outcomes

    def _register(self, text: str) -> _PendingText:
        """Queue one text for the next underlying batch and open a coalescing window."""

        entry = _PendingText(text=text, future=asyncio.get_running_loop().create_future())
        entry.future.add_done_callback(_discard_unretrieved_exception)
        self._queued[text] = entry
        self._schedule_flush()
        return entry

    def _schedule_flush(self) -> None:
        """Ensure one flush task is waiting to close the current coalescing window."""

        if self._flush_task is not None:
            return
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        """Close the coalescing window and send every queued text in bounded batches."""

        aborted = False
        try:
            await asyncio.sleep(self._coalesce_seconds)
            while self._queued:
                await self._run_batch(self._take_batch())
        except asyncio.CancelledError:
            aborted = True
            self._abort(asyncio.CancelledError())
            raise
        except Exception as error:
            aborted = True
            self._abort(error)
        finally:
            self._flush_task = None
            if not aborted and self._queued:
                self._schedule_flush()

    def _take_batch(self) -> list[_PendingText]:
        """Move up to one bounded batch of queued texts into flight."""

        batch: list[_PendingText] = []
        while self._queued and len(batch) < self._max_batch_size:
            text = next(iter(self._queued))
            entry = self._queued.pop(text)
            self._inflight[text] = entry
            batch.append(entry)
        return batch

    async def _run_batch(self, batch: list[_PendingText]) -> None:
        """Send one batch to the base model and settle every waiter of that batch."""

        texts = tuple(entry.text for entry in batch)
        try:
            result = await self._call(texts)
        except asyncio.CancelledError:
            self._fail(batch, asyncio.CancelledError())
            raise
        except Exception as error:
            self._fail(batch, error)
            return
        vectors = result.vectors
        if len(vectors) != len(texts):
            self._fail(batch, ValueError(f"embedding batch returned {len(vectors)} vectors for {len(texts)} texts"))
            return
        outcome = _BatchOutcome(usage=result.usage)
        for entry, vector in zip(batch, vectors, strict=True):
            self._inflight.pop(entry.text, None)
            self._store_vector(entry.text, vector)
            entry.future.set_result(_TextOutcome(vector=vector, batch=outcome))

    def _fail(self, batch: list[_PendingText], error: BaseException) -> None:
        """Settle one failed batch without caching it, so later calls can retry."""

        for entry in batch:
            self._inflight.pop(entry.text, None)
            entry.future.set_exception(error)

    def _abort(self, error: BaseException) -> None:
        """Settle every unresolved text when the pool itself stops."""

        unresolved = [*self._queued.values(), *self._inflight.values()]
        self._queued.clear()
        self._inflight.clear()
        for entry in unresolved:
            if isinstance(error, asyncio.CancelledError):
                entry.future.cancel()
            else:
                entry.future.set_exception(error)

    def _cached_vector(self, text: str) -> EmbeddingVector | None:
        """Return a cached vector and refresh its recent-use position."""

        if self._cache_size < 1 or text not in self._cache:
            return None
        self._cache.move_to_end(text)
        return self._cache[text]

    def _store_vector(self, text: str, vector: EmbeddingVector) -> None:
        """Cache one successful vector, evicting the least recently used entry."""

        if self._cache_size < 1:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


class CoalescingEmbeddingModel:
    """Embed texts through one shared pool that coalesces requests and caches vectors."""

    def __init__(
        self,
        base: EmbeddingModel,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if not hasattr(base, "profile") or not callable(getattr(base, "embed", None)):
            raise ValueError("base model must expose a profile and an embed coroutine")  # noqa: TRY003
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be positive")  # noqa: TRY003
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must not be negative")  # noqa: TRY003
        if cache_size < 0:
            raise ValueError("cache_size must not be negative")  # noqa: TRY003
        self._base = base
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000
        self._cache_size = cache_size
        self._documents = _CoalescingPool(
            base.embed,
            max_batch_size=max_batch_size,
            coalesce_seconds=self._coalesce_seconds,
            cache_size=cache_size,
        )
        self._queries: _CoalescingPool | None = None

    @property
    def profile(self) -> EmbeddingProfile:
        """Expose the deployment-fixed profile of the wrapped model."""

        return self._base.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed an ordered text batch, keeping the input order and every duplicate."""

        return await self._documents.embed(texts)

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed retrieval queries through the base query path when it defines one."""

        return await self._query_pool().embed(texts)

    def _query_pool(self) -> _CoalescingPool:
        """Return the pool that serves queries, falling back to document embeddings."""

        if self._queries is None:
            query_call = getattr(self._base, "embed_query", None)
            if callable(query_call):
                self._queries = _CoalescingPool(
                    query_call,
                    max_batch_size=self._max_batch_size,
                    coalesce_seconds=self._coalesce_seconds,
                    cache_size=self._cache_size,
                )
            else:
                self._queries = self._documents
        return self._queries


__all__ = ["CoalescingEmbeddingModel"]
