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

"""Wire protocol for complete and incremental context transfers.

A Context Pack carries one prepared context as a sequence of bounded chunks
plus the digest and byte count a receiver verifies. A Context Pack Delta
carries only what the receiver cannot already hold: the number of leading
chunks it may keep from the base pack it kept last round, and the chunks that
replace the rest. Both objects serialize to canonical JSON so a receiver can
rebuild the target pack from wire bytes alone.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from powercontext.errors import PowerContextError

CONTEXT_PACK_SCHEMA: Final = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA: Final = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES: Final = 512

_PACK_FIELDS: Final = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS: Final = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_BYTE_COUNT_STEPS: Final = 8
_UTF8_CONTINUATION: Final = 0x80
_UTF8_CONTINUATION_MASK: Final = 0xC0


class ContextPackError(PowerContextError, ValueError):
    """Raised when a Context Pack or delta violates the transfer protocol."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"invalid context pack: {code}")


@dataclass(frozen=True)
class ContextPack:
    """One complete prepared context split into transferable chunks.

    ``digest`` covers the schema and the chunk bytes only, so a receiver can
    verify the content it rebuilt before it trusts it. ``byte_count`` is the
    size of the serialized pack including the field itself.
    """

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        """Build one pack from chunk bytes."""
        chunks = _normalize_items(items)
        digest = _pack_digest(chunks)
        return cls(items=chunks, digest=digest, byte_count=_pack_byte_count(chunks, digest))

    @classmethod
    def from_content(cls, content: str | bytes | None, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Split prepared content into deterministic chunks that keep the original bytes.

        Every chunk stays within ``chunk_bytes`` and decodes as UTF-8 on its
        own, so a receiver can hold and reuse one chunk without the rest.
        """
        return cls.build(_split_content(content, chunk_bytes=chunk_bytes))

    def to_bytes(self) -> bytes:
        """Serialize the pack into its canonical wire form."""
        return _canonical_json({
            "schema": CONTEXT_PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        })

    @classmethod
    def from_bytes(cls, payload: bytes) -> ContextPack:
        """Decode one wire pack and verify its digest and byte count."""
        document = _decode_document(payload)
        if set(document) != _PACK_FIELDS:
            raise ContextPackError("pack-fields")
        if document["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("pack-schema")
        items = _decode_items(document["items"])
        digest = document["digest"]
        if not isinstance(digest, str) or not digest:
            raise ContextPackError("pack-digest")
        if digest != _pack_digest(items):
            raise ContextPackError("pack-digest")
        byte_count = document["byte_count"]
        if type(byte_count) is not int:
            raise ContextPackError("pack-byte-count")
        if byte_count != _pack_byte_count(items, digest):
            raise ContextPackError("pack-byte-count")
        return cls(items=items, digest=digest, byte_count=byte_count)


@dataclass(frozen=True)
class ContextPackDelta:
    """One incremental update that rebuilds a target pack from a base pack.

    ``prefix_items`` counts the leading chunks of the base pack the receiver
    keeps; ``appended`` carries the chunks that replace the remaining ones.
    """

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Carry the shared chunk prefix of two packs and the target suffix."""
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("delta-types")
        prefix = 0
        for base_item, target_item in zip(base.items, target.items, strict=False):
            if base_item != target_item:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    def to_bytes(self) -> bytes:
        """Serialize the delta into its canonical wire form."""
        return _canonical_json({
            "schema": CONTEXT_PACK_DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        })

    @property
    def byte_count(self) -> int:
        """The serialized size of this delta.

        The size is reported, never written: the wire form carries exactly the
        five protocol fields.
        """
        return len(self.to_bytes())

    @classmethod
    def from_bytes(cls, payload: bytes) -> ContextPackDelta:
        """Decode one wire delta and verify its fields."""
        document = _decode_document(payload)
        if set(document) != _DELTA_FIELDS:
            raise ContextPackError("delta-fields")
        if document["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("delta-schema")
        base_digest = document["base_digest"]
        if not isinstance(base_digest, str) or not base_digest:
            raise ContextPackError("delta-base-digest")
        target_digest = document["target_digest"]
        if not isinstance(target_digest, str) or not target_digest:
            raise ContextPackError("delta-target-digest")
        prefix_items = document["prefix_items"]
        if type(prefix_items) is not int or prefix_items < 0:
            raise ContextPackError("delta-prefix")
        appended = _decode_items(document["appended"])
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from the base pack the receiver kept."""
    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta-types")
    if base.digest != delta.base_digest:
        raise ContextPackError("delta-base-digest")
    if delta.prefix_items < 0 or delta.prefix_items > len(base.items):
        raise ContextPackError("delta-prefix-range")
    target = ContextPack.build((*base.items[: delta.prefix_items], *delta.appended))
    if target.digest != delta.target_digest:
        raise ContextPackError("delta-target-digest")
    return target


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: Iterable[bytes]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _normalize_items(items: Iterable[bytes]) -> tuple[bytes, ...]:
    chunks = tuple(items)
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise ContextPackError("pack-items")
    return chunks


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("pack-items")
    chunks: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError("pack-items")
        try:
            chunks.append(base64.b64decode(entry, validate=True))
        except ValueError as error:
            raise ContextPackError("pack-encoding") from error
    return tuple(chunks)


def _decode_document(payload: bytes | str) -> dict[str, object]:
    if isinstance(payload, (bytes, bytearray, memoryview)):
        try:
            text = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextPackError("payload-encoding") from error
    elif isinstance(payload, str):
        text = payload
    else:
        raise ContextPackError("payload-encoding")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ContextPackError("payload-json") from error
    if not isinstance(document, dict):
        raise ContextPackError("payload-object")
    return document


def _pack_digest(items: tuple[bytes, ...]) -> str:
    canonical = _canonical_json({"schema": CONTEXT_PACK_SCHEMA, "items": _encode_items(items)})
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _pack_byte_count(items: tuple[bytes, ...], digest: str) -> int:
    """Resolve the self-referential byte count of one pack.

    The field counts itself, so its own digits change the payload size. The
    size only depends on the digit count of the value, so the fixed point is
    reached after a few steps.
    """
    encoded = _encode_items(items)
    count = 0
    for _ in range(_BYTE_COUNT_STEPS):
        size = len(
            _canonical_json({
                "schema": CONTEXT_PACK_SCHEMA,
                "items": encoded,
                "digest": digest,
                "byte_count": count,
            })
        )
        if size == count:
            return count
        count = size
    raise ContextPackError("pack-byte-count")


def _split_content(content: str | bytes | None, *, chunk_bytes: int) -> tuple[bytes, ...]:
    if type(chunk_bytes) is not int or chunk_bytes < 1:
        raise ContextPackError("chunk-limit")
    if content is None:
        return ()
    if isinstance(content, str):
        data = content.encode("utf-8")
    elif isinstance(content, (bytes, bytearray, memoryview)):
        data = bytes(content)
    else:
        raise ContextPackError("chunk-content")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("chunk-encoding") from error
    return tuple(_split_chunks(data, chunk_bytes))


def _split_chunks(data: bytes, limit: int) -> list[bytes]:
    """Cut one UTF-8 payload into chunks that fit the limit and decode alone."""
    chunks: list[bytes] = []
    start = 0
    size = len(data)
    while start < size:
        end = min(start + limit, size)
        while end < size and end > start and (data[end] & _UTF8_CONTINUATION_MASK) == _UTF8_CONTINUATION:
            end -= 1
        if end == start:
            raise ContextPackError("chunk-boundary")
        chunks.append(data[start:end])
        start = end
    return chunks


__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "DEFAULT_CHUNK_BYTES",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
