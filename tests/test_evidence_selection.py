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

"""Behaviour of the query-driven evidence selection in the prepared context."""

from __future__ import annotations

import json

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)

MEMORY_REF = ArtifactRef(family="memory", artifact_id="memory", revision=3)
BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
PREFIX = "General administrative notes. " * 70


def _hit(entry_id: str, text: str) -> MemoryHit:
    return MemoryHit(
        memory_ref=MEMORY_REF,
        entry_id=entry_id,
        entry_version_id=f"{entry_id}-v1",
        text=text,
        score=1.0,
        matched_by=("fts",),
    )


def _candidates(*hits: MemoryHit, scope_id: str = "project:test") -> tuple[PreparedMemoryCandidates, ...]:
    return (PreparedMemoryCandidates(scope_id=scope_id, memory_ref=MEMORY_REF, hits=hits),)


def _envelope(content: str | None) -> dict[str, object]:
    assert content is not None
    return json.loads(content.splitlines()[-2])


def test_evidence_selection_delivers_the_query_matched_span_with_its_exact_range() -> None:
    text = PREFIX + "The cobalt sensor is calibrated at 37 degrees. "
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor calibrated", max_bytes=1200, evidence_selection=True),
        current_scope_id="project:test",
        memory_candidates=_candidates(_hit("alpha", text)),
    )

    assert build.fragments
    fragment = next(fragment for fragment in build.fragments if "37" in fragment.text)
    assert fragment.text == text[fragment.start : fragment.end]
    assert 0 <= fragment.start < fragment.end <= len(text)
    assert fragment.address.entry_id == "alpha"
    assert fragment.address.memory.scope_id == "project:test"
    assert build.origins == tuple(fragment.address for fragment in build.fragments)


def test_evidence_selection_follows_the_query_within_one_candidate_set() -> None:
    first = PREFIX + "The cobalt sensor is calibrated at 37 degrees. "
    second = PREFIX + "The amber valve opens at 19 degrees. "
    candidates = _candidates(_hit("alpha", first), _hit("beta", second))

    cobalt = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor calibrated", max_bytes=1200, evidence_selection=True),
        current_scope_id="project:test",
        memory_candidates=candidates,
    )
    amber = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="amber valve opens", max_bytes=1200, evidence_selection=True),
        current_scope_id="project:test",
        memory_candidates=candidates,
    )

    assert any("37" in fragment.text for fragment in cobalt.fragments)
    assert not any("19" in fragment.text for fragment in cobalt.fragments)
    assert any("19" in fragment.text for fragment in amber.fragments)
    assert not any("37" in fragment.text for fragment in amber.fragments)


def test_evidence_selection_charges_the_whole_output_against_the_byte_budget() -> None:
    text = "海洋观测 station records temperatures. " * 40
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="海洋观测 station", max_bytes=1200, evidence_selection=True),
        current_scope_id="project:test",
        memory_candidates=_candidates(_hit("one", text)),
    )

    assert build.context.content is not None
    content = build.context.content
    assert build.context.content_bytes == len(content.encode("utf-8"))
    assert build.context.content_bytes <= 1200
    assert BEGIN_MARKER in content
    assert END_MARKER in content

    envelope = _envelope(content)
    assert envelope["trust"] == "untrusted_history"
    items = envelope["items"]
    assert len(items) == len(build.fragments) == len(build.origins)
    for item, fragment in zip(items, build.fragments, strict=True):
        assert item["content"] == fragment.text
        assert item["start"] == fragment.start
        assert item["end"] == fragment.end
        assert item["citation"]["entry_id"] == fragment.address.entry_id
        assert item["citation"]["scope_id"] == fragment.address.memory.scope_id


def test_ordinary_prepare_keeps_whole_entries_when_evidence_selection_is_off() -> None:
    text = "The cobalt sensor is calibrated at 37 degrees."
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor calibrated", max_bytes=4000),
        current_scope_id="project:test",
        memory_candidates=_candidates(_hit("alpha", text)),
    )

    assert build.fragments == ()
    assert build.context.content is not None
    items = _envelope(build.context.content)["items"]
    assert len(items) == 1
    assert items[0]["content"] == text
    assert items[0]["truncated"] is False


def test_evidence_selection_keeps_the_same_text_apart_across_scopes() -> None:
    text = "The cobalt sensor is calibrated at 37 degrees."
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor calibrated", max_bytes=1200, evidence_selection=True),
        current_scope_id="project:first",
        memory_candidates=(
            PreparedMemoryCandidates(scope_id="project:first", memory_ref=MEMORY_REF, hits=(_hit("alpha", text),)),
            PreparedMemoryCandidates(scope_id="project:second", memory_ref=MEMORY_REF, hits=(_hit("alpha", text),)),
        ),
    )

    identities = {(fragment.address.memory.scope_id, fragment.address.entry_id) for fragment in build.fragments}
    assert identities == {("project:first", "alpha"), ("project:second", "alpha")}


def test_evidence_selection_returns_an_empty_context_when_no_span_matches() -> None:
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt", max_bytes=1200, evidence_selection=True),
        current_scope_id="project:test",
        memory_candidates=_candidates(_hit("alpha", "The amber valve opens at 19 degrees.")),
    )

    assert build.context.status == "empty"
    assert build.context.content is None
    assert build.fragments == ()
    assert build.origins == ()
