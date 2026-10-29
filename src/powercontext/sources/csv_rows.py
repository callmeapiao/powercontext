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

"""Row-level CSV capture with stable record identity and standard text evidence."""

from __future__ import annotations

import csv
import io
import json
from typing import Final
from urllib.parse import quote

from pydantic import BaseModel, field_validator, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME: Final = "csv-row"
CSV_ROW_SOURCE_VERSION: Final = "1"
CSV_ROW_IDENTITY_PREFIX: Final = "csv"
_UTF8_BOM: Final = "\ufeff"


class CSVDocumentInput(BaseModel):
    """One offline CSV document addressed by its primary key column."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One captured CSV record that keeps its original column order and values."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @field_validator("primary_key_value")
    @classmethod
    def reject_blank_primary_key_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("primary key values must not be blank")  # noqa: TRY003
        return value

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("CSV rows must expose at least one column")  # noqa: TRY003
        if len(set(value)) != len(value):
            raise ValueError("CSV column names must be unique")  # noqa: TRY003
        for name in value:
            if not name.strip():
                raise ValueError("CSV column names must not be blank")  # noqa: TRY003
        return value

    @model_validator(mode="after")
    def validate_row(self) -> CSVRowInput:
        if len(self.columns) != len(self.values):
            raise ValueError("CSV row values must match the column count")  # noqa: TRY003
        return self


class CSVRowSource(Source):
    """A captured CSV row that retrieval citations can point back to."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_identity(self) -> CSVRowSource:
        if len(self.columns) != len(self.values):
            raise ValueError("CSV row values must match the column count")  # noqa: TRY003
        expected = csv_row_source_id(self.document_id, self.primary_key_value)
        if self.source_id != expected:
            raise ValueError("CSV row identity does not match its document and primary key value")  # noqa: TRY003
        return self


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the stable identity shared by one captured row and its citations."""
    if not isinstance(document_id, str) or not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty trimmed string")  # noqa: TRY003
    if not isinstance(primary_key_value, str):
        # Identity failures stay ValueError so every rejection shares one contract.
        raise ValueError("primary_key_value must be a string")  # noqa: TRY003, TRY004
    source_id = f"{CSV_ROW_IDENTITY_PREFIX}:{_percent_encode(document_id)}:{_percent_encode(primary_key_value)}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse one offline CSV document into validated row inputs.

    The whole document is parsed and validated before any row is returned, so a
    malformed document fails atomically instead of yielding partial rows.
    """
    document_id = value.document_id
    if not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty trimmed string")  # noqa: TRY003
    primary_key = value.primary_key
    if not primary_key:
        raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003

    records = _read_records(value.content)
    columns = _validate_header(records[0] if records else None, primary_key)

    rows: list[CSVRowInput] = []
    seen: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(columns):
            raise ValueError("CSV data record width must match the header width")  # noqa: TRY003
        values = tuple(record)
        primary_key_value = dict(zip(columns, values, strict=True))[primary_key]
        if not primary_key_value.strip():
            raise ValueError("primary key values must not be blank")  # noqa: TRY003
        if primary_key_value in seen:
            raise ValueError("primary key values must be unique within one document")  # noqa: TRY003
        seen.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=document_id,
                primary_key=primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=values,
            )
        )
    return tuple(rows)


class CSVRowAdapter:
    """Resolve and read one captured CSV row."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one captured CSV row without coupling consumers to ``CSVRowSource``."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version=CSV_ROW_SOURCE_VERSION,
    projections=(CSVRowTextEvidenceProjection(),),
)


def _percent_encode(component: str) -> str:
    return quote(component, safe="")


def _read_records(content: bytes) -> list[list[str]]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error  # noqa: TRY003
    if text.startswith(_UTF8_BOM):
        text = text[1:]
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        return list(reader)
    except csv.Error as error:
        raise ValueError(f"CSV content is malformed: {error}") from error  # noqa: TRY003


def _validate_header(record: list[str] | None, primary_key: str) -> tuple[str, ...]:
    if record is None:
        raise ValueError("CSV document must contain a header row")  # noqa: TRY003
    if not record:
        raise ValueError("CSV header row must not be empty")  # noqa: TRY003
    for name in record:
        if not name.strip():
            raise ValueError("CSV header column names must not be blank")  # noqa: TRY003
    if len(set(record)) != len(record):
        raise ValueError("CSV header column names must be unique")  # noqa: TRY003
    if primary_key not in record:
        raise ValueError("primary_key must match one header column exactly")  # noqa: TRY003
    return tuple(record)


__all__ = [
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSV_ROW_SOURCE_VERSION",
    "CSVDocumentInput",
    "CSVRowAdapter",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]
