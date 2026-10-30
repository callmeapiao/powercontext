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

"""Rerank coarse Memory candidates with one external HTTP scoring service.

The service accepts bounded candidate batches and answers with one score per
requested rank. Batches are only a transport concern: the final selection is
made once over the whole candidate pool, so a strong candidate scored in a late
batch still competes with the ones scored earlier. Ranks keep their one-based
position in the complete pool, so a selected rank always names the same Memory
entry and version it named before the rerank.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision, _validate_bounds
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"

_RESPONSE_FIELDS = frozenset({"scores"})
_SCORE_FIELDS = frozenset({"rank", "score"})


class HTTPMemoryReranker:
    """Select final Memory hits with the scores of an external HTTP reranking service."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANK_POLICY_ID,
    ) -> None:
        if batch_size < 1:
            raise ValueError("http rerank batch size must be positive")  # noqa: TRY003
        self.policy_id = policy_id
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate and keep the globally best ``limit`` original ranks.

        ``discarded_rank_count`` reports how many scored candidates did not reach
        the final selection, because every candidate is scored but only ``limit``
        of them are kept.
        """

        _validate_bounds(len(candidates), limit)
        batches = _candidate_batches(candidates, self._batch_size)
        scored: list[tuple[int, float]] = []
        for batch in batches:
            scored.extend(await self._score_batch(query, batch))
        ordered = sorted(scored, key=lambda item: (-item[1], item[0]))
        selected = tuple(rank for rank, _ in ordered[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected,
            usage=InferenceUsage(requests=len(batches)),
            discarded_rank_count=len(candidates) - len(selected),
        )

    async def _score_batch(
        self,
        query: str,
        batch: tuple[tuple[int, MemoryHit], ...],
    ) -> list[tuple[int, float]]:
        """Score one candidate batch and return its ``(rank, score)`` pairs."""

        payload = {
            "query": query,
            "candidates": [{"rank": rank, "text": candidate.text} for rank, candidate in batch],
        }
        response = await self._client.post(self._endpoint, json=payload)
        response.raise_for_status()
        records = _response_scores(_response_json(response))
        batch_scores = [_score_record(record) for record in records]
        _validate_coverage(batch_scores, tuple(rank for rank, _ in batch))
        return batch_scores


def _candidate_batches(
    candidates: tuple[MemoryHit, ...],
    batch_size: int,
) -> tuple[tuple[tuple[int, MemoryHit], ...], ...]:
    """Split the pool into batches that keep the one-based rank of the full pool.

    Ranks are assigned before the split, so a batch never renumbers its own
    candidates and every rank of the pool is sent exactly once.
    """

    return tuple(
        tuple(
            (start + offset + 1, candidate) for offset, candidate in enumerate(candidates[start : start + batch_size])
        )
        for start in range(0, len(candidates), batch_size)
    )


def _response_json(response: httpx.Response) -> object:
    """Decode the response body, reporting an undecodable body as a contract violation."""

    try:
        return response.json()
    except ValueError as error:
        raise ValueError("http rerank response was not valid JSON") from error  # noqa: TRY003


def _response_scores(payload: object) -> list[object]:
    """Validate the response envelope and return its raw score records.

    A malformed service answer is a contract violation, not a programming
    mistake, so every rejection of this module is a ``ValueError``.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("http rerank response must be a JSON object")  # noqa: TRY003, TRY004
    if set(payload.keys()) != _RESPONSE_FIELDS:
        raise ValueError("http rerank response must contain exactly the 'scores' field")  # noqa: TRY003
    records = payload["scores"]
    if not isinstance(records, list):
        raise ValueError("http rerank response 'scores' must be a list")  # noqa: TRY003, TRY004
    return records


def _score_record(record: object) -> tuple[int, float]:
    """Validate one score record and return its rank and its finite score."""

    if not isinstance(record, Mapping):
        raise ValueError("http rerank score record must be a JSON object")  # noqa: TRY003, TRY004
    if set(record.keys()) != _SCORE_FIELDS:
        raise ValueError("http rerank score record must contain exactly the 'rank' and 'score' fields")  # noqa: TRY003
    rank = record["rank"]
    if isinstance(rank, bool) or not isinstance(rank, int):
        raise ValueError("http rerank score 'rank' must be an integer")  # noqa: TRY003, TRY004
    score = record["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("http rerank score must be a number")  # noqa: TRY003, TRY004
    try:
        value = float(score)
    except (OverflowError, ValueError) as error:
        raise ValueError("http rerank score must be a finite number") from error  # noqa: TRY003
    if not math.isfinite(value):
        raise ValueError("http rerank score must be a finite number")  # noqa: TRY003
    return rank, value


def _validate_coverage(scored: list[tuple[int, float]], expected_ranks: tuple[int, ...]) -> None:
    """Require exactly one score for every rank of the batch and nothing else."""

    ranks = [rank for rank, _ in scored]
    if len(ranks) != len(set(ranks)):
        raise ValueError("http rerank response repeated a rank")  # noqa: TRY003
    if set(ranks) != set(expected_ranks):
        raise ValueError("http rerank response must cover exactly the ranks of the requested batch")  # noqa: TRY003


__all__ = ["HTTP_RERANK_POLICY_ID", "HTTPMemoryReranker"]
