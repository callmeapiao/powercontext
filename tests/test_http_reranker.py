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

"""Behavior tests for the HTTP Memory reranking adapter."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def _hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="memory", revision=1),
        entry_id=f"entry-{rank}",
        entry_version_id=f"entry-{rank}-v1",
        text=f"Candidate {rank}",
        score=1 / rank,
        matched_by=("fts",),
    )


def _candidates(count: int) -> tuple[MemoryHit, ...]:
    return tuple(_hit(rank) for rank in range(1, count + 1))


def test_http_reranker_batches_by_pool_rank_and_selects_globally() -> None:
    sent: list[dict[str, object]] = []
    scores = {1: 0.2, 2: 0.9, 3: 0.9, 4: 0.9, 5: 0.1}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(
            200,
            json={"scores": [{"rank": item["rank"], "score": scores[item["rank"]]} for item in body["candidates"]]},
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid/score", client=client, batch_size=2)

            decision = await reranker.rerank("query", _candidates(5), 3)

        assert decision.selected_ranks == (2, 3, 4)
        assert decision.usage.requests == 3
        assert decision.discarded_rank_count == 2
        assert decision.used_fallback is False
        assert len(sent) == 3
        assert [len(body["candidates"]) for body in sent] == [2, 2, 1]
        assert sorted(item["rank"] for body in sent for item in body["candidates"]) == [1, 2, 3, 4, 5]
        for body in sent:
            for item in body["candidates"]:
                assert item["text"] == _hit(item["rank"]).text

    asyncio.run(scenario())


def test_http_reranker_accepts_out_of_order_scores() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scores": [{"rank": 2, "score": 0.1}, {"rank": 1, "score": 0.8}]})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid/score", client=client, batch_size=2)

            decision = await reranker.rerank("query", _candidates(2), 1)

        assert decision.selected_ranks == (1,)
        assert decision.usage.requests == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"scores": [{"rank": 1, "score": 0.5}], "extra": True},
        {"scores": [{"rank": 1, "score": 0.5}], "scores_count": 1},
        {"scores": [{"rank": 2, "score": 0.5}]},
        {"scores": [{"rank": 1, "score": 0.5}, {"rank": 1, "score": 0.4}]},
        {"scores": [{"rank": 1, "score": 0.5, "extra": True}]},
        {"scores": [{"rank": 1, "score": float("nan")}]},
        {"scores": [{"rank": 1, "score": float("inf")}]},
        {"scores": [{"rank": 1}]},
        {"scores": [{"score": 0.5}]},
        {"scores": []},
    ],
)
def test_http_reranker_rejects_invalid_response_contracts(payload: dict[str, object]) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=json.dumps(payload).encode()))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://reranker.invalid/score", client=client).rerank("query", (_hit(1),), 1)

    asyncio.run(scenario())


def test_http_reranker_rejects_a_body_that_is_not_json() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not json"))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://reranker.invalid/score", client=client).rerank("query", (_hit(1),), 1)

    asyncio.run(scenario())


def test_http_reranker_rejects_a_score_for_a_rank_of_another_batch() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scores": [{"rank": 3, "score": 0.5}]})

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid/score", client=client, batch_size=2)
            with pytest.raises(ValueError):
                await reranker.rerank("query", _candidates(3), 1)

    asyncio.run(scenario())


def test_http_reranker_validates_bounds() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"scores": [{"rank": 1, "score": 0.5}]})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid/score", client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (), 1)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (_hit(1),), 2)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (_hit(1),), 0)
            with pytest.raises(ValueError):
                HTTPMemoryReranker("https://reranker.invalid/score", client=client, batch_size=0)

    asyncio.run(scenario())


def test_http_reranker_preserves_http_status_errors() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(503, json={}))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await HTTPMemoryReranker("https://reranker.invalid/score", client=client).rerank("query", (_hit(1),), 1)

    asyncio.run(scenario())


def test_http_reranker_preserves_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.ConnectError):
                await HTTPMemoryReranker("https://reranker.invalid/score", client=client).rerank("query", (_hit(1),), 1)

    asyncio.run(scenario())
