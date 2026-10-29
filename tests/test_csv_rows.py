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

"""Contract tests for the row-level CSV Source."""

from __future__ import annotations

import asyncio
import json

import pytest
from powercontext.sources import SourceDefinitionRegistry, TextEvidence, manifest_for_definition
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def _parse(content: bytes, *, document_id: str = "doc", primary_key: str = "id") -> tuple[CSVRowInput, ...]:
    return parse_csv_document(
        CSVDocumentInput(document_id=document_id, primary_key=primary_key, content=content)
    )


def test_definition_declares_the_documented_identity() -> None:
    assert CSV_ROW_SOURCE_DEFINITION.name == "csv-row"
    assert CSV_ROW_SOURCE_DEFINITION.version == "1"
    assert CSV_ROW_SOURCE_DEFINITION.input_class is CSVRowInput
    assert CSV_ROW_SOURCE_DEFINITION.source_class is CSVRowSource
    assert [projection.name for projection in CSV_ROW_SOURCE_DEFINITION.projections] == [
        TEXT_EVIDENCE_PROJECTION_KEY.name
    ]


def test_values_keep_their_original_spelling_and_type() -> None:
    rows = _parse(b"id,count,ratio,flag,empty\n007,1,1.50,TRUE,\n")
    assert rows[0].values == ("007", "1", "1.50", "TRUE", "")
    assert rows[0].primary_key_value == "007"


def test_unicode_content_is_preserved_without_escaping() -> None:
    rows = _parse("id,name\n1,北京首都\n".encode())
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(rows[0]))
    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.content == json.dumps({"id": "1", "name": "北京首都"}, ensure_ascii=False, separators=(",", ":"))
    assert "北京首都" in evidence.content


def test_identity_is_delimiter_safe_and_bounded() -> None:
    assert csv_row_source_id("a:b", "c") != csv_row_source_id("a", "b:c")
    assert len(csv_row_source_id("d" * 100, "k" * 100)) <= 256
    with pytest.raises(ValueError):
        csv_row_source_id("d" * 200, "k" * 200)


def test_document_validation_rejects_broken_documents() -> None:
    with pytest.raises(ValueError):
        _parse(b"")
    with pytest.raises(ValueError):
        _parse(b"\n")
    with pytest.raises(ValueError):
        _parse(b"id,name\n1,one\n2\n")
    with pytest.raises(ValueError):
        _parse(b"id,name\n1,one,extra\n")
    with pytest.raises(ValueError):
        _parse(b"id,name\n1,one\n1,two\n")
    with pytest.raises(ValueError):
        _parse(b"id,name\n   ,one\n")
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="", primary_key="id", content=b"id,name\n1,one\n"))
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="", content=b"id,name\n1,one\n"))


def test_invalid_documents_never_return_partial_rows() -> None:
    with pytest.raises(ValueError):
        _parse(b"id,name\n1,one\n2,two\n3\n")


def test_header_only_documents_are_valid_and_empty_records_are_ignored() -> None:
    assert _parse(b"id,name\n") == ()
    assert _parse(b"id,name\n\n\n1,one\n\n") == _parse(b"id,name\n1,one\n")


def test_captured_rows_persist_as_observations_with_one_identity() -> None:
    rows = _parse(b"id,name\n1,one\n2,two\n")
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    manifest = manifest_for_definition(CSV_ROW_SOURCE_DEFINITION)
    assert manifest.name == "csv-row"
    assert manifest.version == "1"

    identities = []
    for row in rows:
        source = asyncio.run(registry.resolve(row))
        identities.append(source.source_id)
        assert source.name == source.source_id
        assert source.materialization.value == "captured"
        assert asyncio.run(registry.read(source)) == row

    assert identities == ["csv:doc:1", "csv:doc:2"]
    assert len(set(identities)) == len(rows)


def test_projection_keeps_column_order_and_exact_metadata() -> None:
    rows = _parse(b"id,name\n1,one\n")
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(rows[0]))
    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.source_type == "csv-row"
    assert evidence.source_id == "csv:doc:1"
    assert evidence.content == '{"id":"1","name":"one"}'
    assert evidence.metadata == {
        "document_id": "doc",
        "primary_key": "id",
        "primary_key_value": "1",
        "columns": ["id", "name"],
        "row": {"id": "1", "name": "one"},
    }
