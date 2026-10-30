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

"""Pi session branch capture with stable event identity and standard text evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final, NamedTuple
from urllib.parse import quote

from pydantic import BaseModel, JsonValue, field_validator, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME: Final = "pi-event"
PI_EVENT_SOURCE_VERSION: Final = "1"
PI_EVENT_IDENTITY_PREFIX: Final = "pi"
PI_SESSION_CONNECTOR_NAME: Final = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION: Final = "1"
PI_SESSION_FORMAT_VERSION: Final = 3
PI_CHECKPOINT_VERSION: Final = 1
PI_COMPACTION_TYPES: Final = ("compaction", "branch_summary")
_UTF8_BOM: Final = "\ufeff"
_UNKNOWN_TOOL: Final = "unknown"


class PiSessionInput(BaseModel):
    """One offline Pi session file and the branch selected from it."""

    content: bytes
    leaf_id: str | None = None

    @field_validator("leaf_id")
    @classmethod
    def validate_leaf_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("leaf_id must not be blank")  # noqa: TRY003
        return value


class PiEventInput(BaseModel):
    """One visible Pi event with the complete original record it was parsed from."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A captured Pi event that retrieval citations can point back to."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_identity(self) -> PiEventSource:
        if self.name != self.source_id:
            raise ValueError("Pi event identity must match its Source name")  # noqa: TRY003
        expected = pi_event_source_id(self.session_id, self.event_id)
        if self.source_id != expected:
            raise ValueError("Pi event identity does not match its session and event")  # noqa: TRY003
        return self


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the stable identity shared by one captured event and its citations.

    The identity depends on the session id and the event id only, so the same
    event always resolves to the same Source and two events never collide.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")  # noqa: TRY003
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a non-empty string")  # noqa: TRY003
    readable = f"{PI_EVENT_IDENTITY_PREFIX}:{_percent_encode(session_id)}:{_percent_encode(event_id)}"
    if len(readable) <= MAX_SOURCE_ID_LENGTH:
        return readable
    digest = hashlib.sha256(f"{session_id}\x00{event_id}".encode()).hexdigest()
    return f"{PI_EVENT_IDENTITY_PREFIX}:sha256:{digest}"


class _SessionNode(NamedTuple):
    """One validated session record with its resolved parent link."""

    event_id: str
    parent_id: str | None
    record: dict[str, JsonValue]


class _VisibleEvent(NamedTuple):
    """One visible event with the role and body its evidence text is built from."""

    event_type: str
    role: str
    body: str


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse one Pi session file into the visible events of the selected branch.

    The whole file is parsed and validated before any event is returned, so a
    malformed session fails atomically instead of yielding partial history. The
    returned events cover the complete ancestor chain of the selected leaf and
    keep the order from the root to the leaf.
    """
    session_id, nodes = _decode_session(value.content)
    return _branch_events(session_id, nodes, value.leaf_id)


class PiEventAdapter:
    """Resolve and read one captured Pi event."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one captured Pi event without coupling consumers to ``PiEventSource``."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_EVENT_SOURCE_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


class PiSessionConnector:
    """Import the visible events of one selected Pi branch as durable evidence.

    Every run parses the whole session file, submits only the visible events of
    the selected branch that are not imported yet, and reports the imported
    event identities through the checkpoint of the surrounding lifecycle.
    """

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: Path | str, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        """Import the events of the selected branch that are not imported yet."""
        content = _read_session_file(self._path)
        session_id, nodes = _decode_session(content)
        events = _branch_events(session_id, nodes, self._leaf_id)
        imported = _imported_events(session.checkpoint)
        _reject_rewritten_events(session_id, nodes, imported)
        for event in events:
            source_id = pi_event_source_id(event.session_id, event.event_id)
            if source_id in imported:
                continue
            outcome = await session.submit(source_id, PI_EVENT_SOURCE_NAME, event)
            if outcome.status is ConnectorSubmissionStatus.ACCEPTED:
                imported[source_id] = {
                    "session_id": event.session_id,
                    "event_id": event.event_id,
                    "digest": _record_digest(event.record),
                }
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=_checkpoint_payload(session_id, imported),
        )


def _percent_encode(component: str) -> str:
    return quote(component, safe="")


def _read_session_file(path: Path) -> bytes:
    """Read one session file without executing anything it contains."""
    try:
        return path.read_bytes()
    except OSError as error:
        raise ValueError(f"Pi session file cannot be read: {path}") from error  # noqa: TRY003


def _decode_session(content: bytes) -> tuple[str, tuple[_SessionNode, ...]]:
    """Decode one JSONL session file into its session id and validated records."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error  # noqa: TRY003
    if text.startswith(_UTF8_BOM):
        text = text[1:]
    records = tuple(_decode_record(line) for line in text.splitlines() if line.strip())
    if not records:
        raise ValueError("Pi session must contain a session header")  # noqa: TRY003
    header = records[0]
    if header.get("type") != "session":
        raise ValueError("Pi session must start with a session record")  # noqa: TRY003
    version = header.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != PI_SESSION_FORMAT_VERSION:
        raise ValueError("Pi session must declare version 3")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Pi session must declare a non-empty id")  # noqa: TRY003
    return session_id, _validate_records(records[1:])


def _decode_record(line: str) -> dict[str, JsonValue]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f"Pi session contains malformed JSON: {error}") from error  # noqa: TRY003
    if not isinstance(value, dict):
        # Every parse failure stays a ValueError so one contract covers them all.
        raise ValueError("Pi session records must be JSON objects")  # noqa: TRY003, TRY004
    return value


def _validate_records(records: tuple[dict[str, JsonValue], ...]) -> tuple[_SessionNode, ...]:
    """Validate that every record has a unique id and an already known parent."""
    nodes: list[_SessionNode] = []
    known: set[str] = set()
    for record in records:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("Pi session records must declare a non-empty id")  # noqa: TRY003
        if event_id in known:
            raise ValueError(f"Pi session repeats event id {event_id!r}")  # noqa: TRY003
        parent_id = record.get("parentId")
        if isinstance(parent_id, str) and not parent_id.strip():
            parent_id = None
        if parent_id is not None:
            if not isinstance(parent_id, str):
                raise ValueError("Pi session parent ids must be non-empty strings")  # noqa: TRY003
            if parent_id not in known:
                raise ValueError(f"Pi session event {event_id!r} references an unknown parent")  # noqa: TRY003
        known.add(event_id)
        nodes.append(_SessionNode(event_id=event_id, parent_id=parent_id, record=record))
    return tuple(nodes)


def _branch_events(
    session_id: str,
    nodes: tuple[_SessionNode, ...],
    leaf_id: str | None,
) -> tuple[PiEventInput, ...]:
    """Return the visible events on the ancestor chain of the selected leaf."""
    if not nodes:
        return ()
    by_id = {node.event_id: node for node in nodes}
    if leaf_id is None:
        leaf = nodes[-1].event_id
    elif leaf_id in by_id:
        leaf = leaf_id
    else:
        raise ValueError(f"Pi session has no event {leaf_id!r}")  # noqa: TRY003

    chain: list[_SessionNode] = []
    visited: set[str] = set()
    current: str | None = leaf
    while current is not None:
        if current in visited:
            raise ValueError(f"Pi session event {current!r} takes part in a parent cycle")  # noqa: TRY003
        visited.add(current)
        node = by_id[current]
        chain.append(node)
        current = node.parent_id
    chain.reverse()

    events: list[PiEventInput] = []
    for node in chain:
        visible = _visible_event(node.record)
        if visible is None:
            continue
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=node.event_id,
                parent_id=node.parent_id,
                event_type=visible.event_type,
                role=visible.role,
                content=f"{visible.role}: {visible.body}",
                record=node.record,
            )
        )
    return tuple(events)


def _visible_event(record: dict[str, JsonValue]) -> _VisibleEvent | None:
    """Return the visible text of one record, or None when it has no visible body."""
    event_type = record.get("type")
    if not isinstance(event_type, str):
        return None
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        if role == "toolResult":
            body = _tool_result_body(message)
        elif role == "bashExecution":
            body = _bash_body(message)
        else:
            body = _visible_text(message.get("content"))
        return _VisibleEvent(event_type, role, body) if body else None
    if event_type in PI_COMPACTION_TYPES:
        body = _summary_body(record.get("summary"))
        return _VisibleEvent(event_type, event_type, body) if body else None
    if event_type == "custom_message":
        body = _visible_text(record.get("content"))
        return _VisibleEvent(event_type, "custom", body) if body else None
    return None


def _visible_text(content: JsonValue) -> str:
    """Join the visible text of a string or a block list with newlines."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "toolCall":
            parts.append(_tool_call_body(block))
    return "\n".join(parts)


def _tool_call_body(block: dict[str, JsonValue]) -> str:
    """Render one tool call block as its visible evidence line."""
    name = _text_value(block.get("name")) or _UNKNOWN_TOOL
    call_id = _text_value(block.get("id")) or _UNKNOWN_TOOL
    arguments = block.get("arguments")
    payload = json.dumps(
        {} if arguments is None else arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"[tool_call {name} {call_id}] {payload}"


def _tool_result_body(message: dict[str, JsonValue]) -> str:
    """Render one tool result message as its visible evidence line."""
    name = _text_value(message.get("toolName")) or _text_value(message.get("name")) or _UNKNOWN_TOOL
    call_id = _text_value(message.get("toolCallId")) or _text_value(message.get("tool_call_id")) or _UNKNOWN_TOOL
    return f"[tool_result {name} {call_id}] {_visible_text(message.get('content'))}"


def _bash_body(message: dict[str, JsonValue]) -> str:
    """Render one bash execution message as its visible evidence text."""
    command = _text_value(message.get("command")) or ""
    output = message.get("output")
    if not isinstance(output, str):
        output = _visible_text(message.get("content"))
    body = f"$ {command}"
    return f"{body}\n{output}" if output else body


def _summary_body(summary: JsonValue) -> str:
    """Return the visible text of one compaction or branch summary."""
    if isinstance(summary, str):
        return summary
    if isinstance(summary, (dict, list)):
        return json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ""


def _text_value(value: JsonValue) -> str | None:
    """Return one non-blank string value, or None when the field carries no text."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _imported_events(checkpoint: JsonValue | None) -> dict[str, dict[str, JsonValue]]:
    """Read the imported event identities and record digests from one checkpoint."""
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict):
        raise ValueError("Pi session checkpoint must be a JSON object")  # noqa: TRY003, TRY004
    if checkpoint.get("version") != PI_CHECKPOINT_VERSION:
        raise ValueError("Pi session checkpoint version is not supported")  # noqa: TRY003
    imported = checkpoint.get("imported")
    if not isinstance(imported, dict):
        raise ValueError("Pi session checkpoint must contain the imported events")  # noqa: TRY003, TRY004
    events: dict[str, dict[str, JsonValue]] = {}
    for source_id, entry in imported.items():
        if not isinstance(entry, dict):
            raise ValueError("Pi session checkpoint entries must be JSON objects")  # noqa: TRY003, TRY004
        events[source_id] = entry
    return events


def _reject_rewritten_events(
    session_id: str,
    nodes: tuple[_SessionNode, ...],
    imported: dict[str, dict[str, JsonValue]],
) -> None:
    """Reject a session whose already imported records were rewritten."""
    digests = {node.event_id: _record_digest(node.record) for node in nodes}
    for entry in imported.values():
        if entry.get("session_id") != session_id:
            continue
        event_id = entry.get("event_id")
        digest = entry.get("digest")
        if not isinstance(event_id, str) or not isinstance(digest, str):
            raise ValueError("Pi session checkpoint entries must record their event identity")  # noqa: TRY003, TRY004
        current = digests.get(event_id)
        if current is not None and current != digest:
            raise ValueError(f"Pi session event {event_id!r} was rewritten after it was imported")  # noqa: TRY003


def _record_digest(record: dict[str, JsonValue]) -> str:
    """Return the checksum of one complete session record."""
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _checkpoint_payload(session_id: str, imported: dict[str, dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Build the durable checkpoint that records the imported event identities."""
    return {
        "version": PI_CHECKPOINT_VERSION,
        "session_id": session_id,
        "imported": {source_id: dict(entry) for source_id, entry in imported.items()},
    }


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_EVENT_SOURCE_VERSION",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PiEventAdapter",
    "PiEventInput",
    "PiEventSource",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
