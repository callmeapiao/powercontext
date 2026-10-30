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

"""Behavior tests for the coalescing embedding wrapper."""

from __future__ import annotations

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


class RecordingEmbedding:
    """Offline embedding model that records every underlying batch."""

    profile = EmbeddingProfile(profile_id="coalescing-tests", model="local", dimension=1)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
        self.calls.append(texts)
        return EmbeddingResult(
            vectors=tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=1, input_tokens=len(texts) * 3, output_tokens=0),
        )


class BlockingEmbedding(RecordingEmbedding):
    """Offline embedding model that holds every batch until it is released."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
        self.calls.append(texts)
        self.started.set()
        await self.release.wait()
        return EmbeddingResult(vectors=tuple((float(len(text)),) for text in texts))


def test_concurrent_calls_share_one_batch_and_keep_input_order() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=4, coalesce_ms=20, cache_size=4)

        first, second = await asyncio.gather(
            model.embed(("alpha", "beta", "alpha")),
            model.embed(("beta", "gamma")),
        )

        assert first.vectors == ((5.0,), (4.0,), (5.0,))
        assert second.vectors == ((4.0,), (5.0,))
        assert base.calls == [("alpha", "beta", "gamma")]

    asyncio.run(scenario())


def test_cached_vectors_are_reused_and_evicted_by_recent_use() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=2)

        await model.embed(("alpha",))
        await model.embed(("beta",))
        cached = await model.embed(("alpha",))
        await model.embed(("gamma",))
        await model.embed(("beta",))

        assert cached.usage.requests == 0
        assert base.calls == [("alpha",), ("beta",), ("gamma",), ("beta",)]

    asyncio.run(scenario())


def test_disabled_cache_still_shares_running_requests() -> None:
    async def scenario() -> None:
        base = BlockingEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)

        first = asyncio.create_task(model.embed(("shared",)))
        second = asyncio.create_task(model.embed(("shared",)))
        await base.started.wait()
        base.release.set()

        assert (await first).vectors == ((6.0,),)
        assert (await second).vectors == ((6.0,),)
        assert base.calls == [("shared",)]

    asyncio.run(scenario())


def test_late_waiter_joins_a_running_batch() -> None:
    async def scenario() -> None:
        base = BlockingEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)

        first = asyncio.create_task(model.embed(("shared",)))
        await base.started.wait()
        late = asyncio.create_task(model.embed(("shared",)))
        await asyncio.sleep(0.01)
        base.release.set()

        assert (await first).vectors == ((6.0,),)
        assert (await late).vectors == ((6.0,),)
        assert base.calls == [("shared",)]

    asyncio.run(scenario())


def test_failed_batch_is_not_cached_and_can_be_retried() -> None:
    class Flaky(RecordingEmbedding):
        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
            self.calls.append(texts)
            if len(self.calls) == 1:
                raise RuntimeError("temporary")
            return EmbeddingResult(vectors=tuple((1.0,) for _ in texts))

    async def scenario() -> None:
        base = Flaky()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)

        with pytest.raises(RuntimeError):
            await model.embed(("retry",))
        assert (await model.embed(("retry",))).vectors == ((1.0,),)
        assert base.calls == [("retry",), ("retry",)]

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_break_the_other_waiters() -> None:
    async def scenario() -> None:
        base = BlockingEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)

        cancelled = asyncio.create_task(model.embed(("shared",)))
        survivor = asyncio.create_task(model.embed(("shared",)))
        await base.started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        base.release.set()

        assert (await survivor).vectors == ((6.0,),)
        assert base.calls == [("shared",)]

    asyncio.run(scenario())


def test_usage_counts_every_waited_batch_once() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=20)

        result = await model.embed(("alpha", "beta"))

        assert result.usage.requests == 2
        assert result.usage.input_tokens == 6
        assert base.calls == [("alpha",), ("beta",)]

    asyncio.run(scenario())


def test_empty_input_never_reaches_the_base_model() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)

        empty = await model.embed(())

        assert empty.vectors == ()
        assert empty.usage.requests == 0
        assert base.calls == []

    asyncio.run(scenario())


def test_wrapper_exposes_the_base_profile_and_rejects_an_invalid_base() -> None:
    model = CoalescingEmbeddingModel(RecordingEmbedding(), coalesce_ms=0)

    assert model.profile == RecordingEmbedding.profile
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(object(), coalesce_ms=0)
