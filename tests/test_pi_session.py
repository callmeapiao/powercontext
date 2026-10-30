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

"""Contract tests for the Pi session branch Source."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from powercontext.sources import (
    ConnectorBinding,
    ConnectorRunStatus,
    SourceDefinitionRegistry,
    SourceRef,
    TextEvidence,
)
from powercontext.sources.connectors import ConnectorLifecycle
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiEventSource,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
    pi_event_source_id,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

_SESSION_HEADER = {"type": "session", "version": 3, "id": "session-a"}


def _records() -> list[dict[str, Any]]:
    """Return one session that mixes visible events with a control node."""
    return [
        {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "需求"}},
        {"type": "thinking_level_change", "id": "control", "parentId": "root"},
        {
            "type": "message",
            "id": "call",
            "parentId": "control",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "toolCall", "name": "grep", "id": "call-1", "arguments": {"b": 2, "a": "值"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "result",
            "parentId": "call",
            "message": {
                "role": "toolResult",
                "toolName": "grep",
                "toolCallId": "call-1",
                "content": [{"type": "text", "text": "match"}],
            },
        },
        {
            "type": "message",
            "id": "shell",
            "parentId": "result",
            "message": {"role": "bashExecution", "command": "echo hi", "output": "hi", "content": []},
        },
        {"type": "custom_message", "id": "custom", "parentId": "shell", "content": [{"type": "text", "text": "note"}]},
        {"type": "compaction", "id": "compact", "parentId": "custom", "summary": "summary text"},
        {"type": "branch_summary", "id": "branch", "parentId": "compact", "summary": "branch text"},
    ]


def _session(records: list[dict[str, Any]], *, leaf_id: str | None = None) -> tuple[PiEventInput, ...]:
    raw = "\n".join(json.dumps(record) for record in [_SESSION_HEADER, *records]).encode()
    return parse_pi_session(PiSessionInput(content=raw, leaf_id=leaf_id))


def test_definition_declares_the_documented_identity() -> None:
    assert PI_EVENT_SOURCE_DEFINITION.name == "pi-event"
    assert PI_EVENT_SOURCE_DEFINITION.version == "1"
    assert PI_EVENT_SOURCE_DEFINITION.input_class is PiEventInput
    assert PI_EVENT_SOURCE_DEFINITION.source_class is PiEventSource
    assert [projection.name for projection in PI_EVENT_SOURCE_DEFINITION.projections] == [
        TEXT_EVIDENCE_PROJECTION_KEY.name
    ]


def test_selected_branch_keeps_control_nodes_and_renders_visible_text() -> None:
    events = _session(_records())

    assert [event.event_id for event in events] == [
        "root",
        "call",
        "result",
        "shell",
        "custom",
        "compact",
        "branch",
    ]
    assert [event.content for event in events] == [
        "user: 需求",
        'assistant: [tool_call grep call-1] {"a":"值","b":2}',
        "toolResult: [tool_result grep call-1] match",
        "bashExecution: $ echo hi\nhi",
        "custom: note",
        "compaction: summary text",
        "branch_summary: branch text",
    ]
    assert [event.role for event in events] == [
        "user",
        "assistant",
        "toolResult",
        "bashExecution",
        "custom",
        "compaction",
        "branch_summary",
    ]
    assert [event.event_type for event in events] == [
        "message",
        "message",
        "message",
        "message",
        "custom_message",
        "compaction",
        "branch_summary",
    ]
    assert events[1].parent_id == "control"
    assert events[0].parent_id is None


def test_every_visible_event_keeps_its_complete_original_record() -> None:
    records = _records()
    events = _session(records)

    assert [event.record for event in events] == [
        records[0],
        records[2],
        records[3],
        records[4],
        records[5],
        records[6],
        records[7],
    ]


def test_explicit_leaf_selects_another_branch_of_the_same_session() -> None:
    records = [
        *_records(),
        {"type": "message", "id": "sibling", "parentId": "root", "message": {"role": "user", "content": "other"}},
    ]

    selected = _session(records, leaf_id="sibling")

    assert [event.event_id for event in selected] == ["root", "sibling"]
    # Without an explicit leaf the last record of the file is the selected branch tip.
    assert [event.event_id for event in _session(records)][-1] == "sibling"
    assert [event.event_id for event in _session(records[:-1])][-1] == "branch"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}\n",
        b"[]\n",
        b"null\n",
        b"\xff",
        b'{"type":"session","version":2,"id":"x"}\n',
        b'{"type":"session","version":3,"id":""}\n',
        b'{"type":"session","version":3,"id":"x"}\n{"type":"message","id":"a","parentId":"a"}\n',
        b'{"type":"session","version":3,"id":"x"}\n{"type":"message","id":"a","parentId":"gone"}\n',
        b'{"type":"session","version":3,"id":"x"}\n{"type":"message","id":"a","parentId":null}\n'
        b'{"type":"message","id":"a","parentId":null}\n',
        b'{"type":"session","version":3,"id":"x"}\n{"type":"message","id":"a","parentId":null}\nnot json\n',
    ],
)
def test_invalid_sessions_fail_without_partial_events(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=raw))


def test_missing_leaf_is_rejected() -> None:
    with pytest.raises(ValueError):
        _session(_records(), leaf_id="absent")


def test_identity_is_stable_bounded_and_collision_free() -> None:
    assert pi_event_source_id("session-a", "event-1") == pi_event_source_id("session-a", "event-1")
    assert pi_event_source_id("session-a", "event-1") != pi_event_source_id("session-a", "event-2")
    assert pi_event_source_id("a:b", "c") != pi_event_source_id("a", "b:c")
    assert len(pi_event_source_id("s" * 200, "e" * 200)) <= 256


def test_captured_events_expose_one_identity_and_the_standard_text_evidence() -> None:
    events = _session(_records())
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))

    sources = [asyncio.run(registry.resolve(event)) for event in events]
    identities = [source.name for source in sources]

    assert len(set(identities)) == len(events)
    assert sources[0].materialization.value == "captured"
    assert asyncio.run(registry.read(sources[0])) == events[0]
    for source, event in zip(sources, events, strict=True):
        evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
        assert evidence.source_type == "pi-event"
        assert evidence.source_id == source.name
        assert evidence.content == event.content
        assert evidence.metadata == {
            "session_id": event.session_id,
            "event_id": event.event_id,
            "parent_id": event.parent_id,
            "event_type": event.event_type,
            "role": event.role,
        }


class RecordingSink:
    """Accept every submitted event and remember it in submission order."""

    def __init__(self) -> None:
        self.submitted: list[PiEventInput] = []

    async def submit(
        self,
        binding: ConnectorBinding,
        item_id: str,
        definition_name: str,
        value: object,
        /,
    ) -> SourceRef:
        del binding
        assert definition_name == "pi-event"
        assert isinstance(value, PiEventInput)
        self.submitted.append(value)
        return SourceRef(source_type=definition_name, source_id=item_id)


class MemoryCheckpointStore:
    """Keep one durable checkpoint for the binding under test."""

    def __init__(self) -> None:
        self.value: JsonValue | None = None

    async def load(self, binding: ConnectorBinding, /) -> JsonValue | None:
        del binding
        return deepcopy(self.value)

    async def save(
        self,
        binding: ConnectorBinding,
        checkpoint: JsonValue | None,
        /,
        *,
        expected: JsonValue | None,
    ) -> None:
        del binding
        assert self.value == expected
        self.value = deepcopy(checkpoint)


def _binding() -> ConnectorBinding:
    return ConnectorBinding(
        scope_id="scope-a",
        binding_id="pi-a",
        connector_name="pi-session-history",
        connector_version="1",
    )


def _write_session(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record) for record in [_SESSION_HEADER, *records]) + "\n",
        encoding="utf-8",
    )


def _branch_records() -> list[dict[str, Any]]:
    return [
        {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "shared"}},
        {"type": "message", "id": "left", "parentId": "root", "message": {"role": "assistant", "content": "left"}},
        {"type": "model_change", "id": "control", "parentId": "root"},
        {
            "type": "message",
            "id": "right",
            "parentId": "control",
            "message": {"role": "assistant", "content": "right"},
        },
    ]


def test_connector_imports_only_new_events_across_branches_and_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "session.jsonl"
        records = _branch_records()
        _write_session(path, records)
        sink = RecordingSink()
        lifecycle = ConnectorLifecycle(sink=sink, checkpoints=MemoryCheckpointStore())

        first = await lifecycle.run(PiSessionConnector(path, "left"), _binding())
        assert [event.content for event in sink.submitted] == ["user: shared", "assistant: left"]
        assert first.status is ConnectorRunStatus.COMPLETE
        assert first.previous_checkpoint is None

        second = await lifecycle.run(PiSessionConnector(path, "right"), _binding())
        assert [event.content for event in sink.submitted] == [
            "user: shared",
            "assistant: left",
            "assistant: right",
        ]
        assert second.previous_checkpoint == first.committed_checkpoint

        replay = await lifecycle.run(PiSessionConnector(path, "right"), _binding())
        assert len(sink.submitted) == 3
        assert replay.previous_checkpoint == second.committed_checkpoint

        records.append({
            "type": "message",
            "id": "tail",
            "parentId": "right",
            "message": {"role": "assistant", "content": "tail"},
        })
        _write_session(path, records)
        added = await lifecycle.run(PiSessionConnector(path, "tail"), _binding())
        assert [event.content for event in sink.submitted][-1] == "assistant: tail"
        assert added.previous_checkpoint == replay.committed_checkpoint

    asyncio.run(scenario())


def test_connector_rejects_rewritten_history_and_keeps_committed_progress(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "session.jsonl"
        records = _branch_records()
        _write_session(path, records)
        sink = RecordingSink()
        checkpoints = MemoryCheckpointStore()
        lifecycle = ConnectorLifecycle(sink=sink, checkpoints=checkpoints)

        committed = await lifecycle.run(PiSessionConnector(path, "right"), _binding())
        progress = checkpoints.value
        assert progress is not None

        records[0]["message"]["content"] = "rewritten"
        _write_session(path, records)
        with pytest.raises(ValueError):
            await lifecycle.run(PiSessionConnector(path, "right"), _binding())
        assert checkpoints.value == progress
        assert committed.committed_checkpoint == progress

        path.write_text(path.read_text(encoding="utf-8") + "{broken}\n", encoding="utf-8")
        with pytest.raises(ValueError):
            await lifecycle.run(PiSessionConnector(path, "right"), _binding())
        assert checkpoints.value == progress

    asyncio.run(scenario())
