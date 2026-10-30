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

"""Contract tests for the Context Pack transfer protocol."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from powercontext.builtin.runtime import (
    BuiltinConfig,
    MemoryEntryInput,
    PrepareContextRequest,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)
from powercontext.builtin.scope import ScopeDraft


def _wire(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def test_pack_carries_the_documented_wire_fields() -> None:
    items = (b"alpha\n", "海洋".encode())
    unsigned = {
        "schema": "powercontext.context-pack.v1",
        "items": [base64.b64encode(item).decode("ascii") for item in items],
    }
    digest = "sha256:" + hashlib.sha256(_wire(unsigned)).hexdigest()

    pack = ContextPack.build(items)

    assert json.loads(pack.to_bytes()) == {**unsigned, "digest": digest, "byte_count": len(pack.to_bytes())}
    assert pack.digest == digest
    assert pack.byte_count == len(pack.to_bytes())
    assert ContextPack.from_bytes(pack.to_bytes()) == pack


def test_content_chunks_keep_every_byte_inside_the_limit() -> None:
    pack = ContextPack.from_content("alpha 海洋 beta", chunk_bytes=7)

    assert b"".join(pack.items).decode("utf-8") == "alpha 海洋 beta"
    assert all(len(item) <= 7 for item in pack.items)
    assert all(item.decode("utf-8") for item in pack.items)
    assert ContextPack.from_content("").items == ()


def test_content_that_cannot_fit_the_limit_is_rejected() -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("海", chunk_bytes=2)
    with pytest.raises(ContextPackError):
        ContextPack.from_content("alpha", chunk_bytes=0)


def test_delta_reuses_the_shared_prefix_and_rebuilds_the_target() -> None:
    base = ContextPack.build((b"alpha", b"beta"))
    target = ContextPack.build((b"alpha", b"beta", b"gamma"))

    delta = ContextPackDelta.between(base, target)

    assert delta.base_digest == base.digest
    assert delta.target_digest == target.digest
    assert delta.prefix_items == 2
    assert delta.appended == (b"gamma",)
    assert delta.byte_count == len(delta.to_bytes())
    assert apply_context_pack_delta(ContextPack.from_bytes(base.to_bytes()), delta).to_bytes() == target.to_bytes()


def test_delta_rejects_a_base_the_receiver_does_not_hold() -> None:
    base = ContextPack.build((b"one",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"one", b"two")))

    with pytest.raises(ContextPackError):
        apply_context_pack_delta(ContextPack.build((b"other",)), delta)


def test_decoders_reject_payloads_that_break_the_protocol() -> None:
    pack = ContextPack.build((b"alpha",))
    payload = json.loads(pack.to_bytes())

    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(_wire({key: value for key, value in payload.items() if key != "digest"}))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(_wire({**payload, "extra": 1}))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(_wire({**payload, "items": ["not base64!"]}))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(_wire({**payload, "byte_count": payload["byte_count"] + 1}))

    delta = ContextPackDelta.between(pack, ContextPack.build((b"alpha", b"beta")))
    delta_payload = json.loads(delta.to_bytes())
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(_wire({**delta_payload, "prefix_items": -1}))
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(_wire({**delta_payload, "prefix_items": True}))
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(_wire({key: value for key, value in delta_payload.items() if key != "appended"}))


def test_runtime_prepares_an_incremental_pack_that_rebuilds_the_content() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig()) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Incremental pack", summary="Runtime contract", idempotency_key="context-pack-runtime")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            context = runtime.context.for_scope(scope.scope_id)
            await memory.remember(
                RememberMemoryRequest(
                    entries=(MemoryEntryInput(kind="conversation-turn", text="The remote control lagged."),)
                )
            )

            first = await context.prepare_incremental(PrepareContextRequest(query="remote", max_bytes=4096))
            assert isinstance(first, ContextPack)
            await memory.remember(
                RememberMemoryRequest(
                    entries=(MemoryEntryInput(kind="conversation-turn", text="The remote control recovered."),)
                )
            )

            second = await context.prepare_incremental(
                PrepareContextRequest(query="remote", max_bytes=4096), base_pack=first
            )
            assert isinstance(second, ContextPackDelta)
            expected = await context.prepare(PrepareContextRequest(query="remote", max_bytes=4096))

        rebuilt = apply_context_pack_delta(
            ContextPack.from_bytes(first.to_bytes()),
            ContextPackDelta.from_bytes(second.to_bytes()),
        )
        assert b"".join(rebuilt.items).decode("utf-8") == expected.content
        assert expected.content

    asyncio.run(scenario())
