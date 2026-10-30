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

"""Contract tests for the page-level PDF Source."""

from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    PDFPageSource,
    parse_pdf_document,
    pdf_page_source_id,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

pypdf = pytest.importorskip("pypdf")
PdfWriter = pypdf.PdfWriter


def _pdf_document(pages: list[str | None]) -> bytes:
    """Build a minimal PDF whose pages carry the given text lines."""

    bodies: dict[int, bytes] = {}
    counter = 0

    def add(body: bytes) -> int:
        nonlocal counter
        counter += 1
        bodies[counter] = body
        return counter

    catalog_number = add(b"")
    pages_number = add(b"")
    font_number = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_numbers = []
    for text in pages:
        content_number = None
        if text is not None:
            stream = f"BT /F1 12 Tf 10 150 Td ({text}) Tj ET".encode("latin-1")
            content_number = add(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        body = (
            f"<< /Type /Page /Parent {pages_number} 0 R /MediaBox [0 0 200 200] "
            f"/Resources << /Font << /F1 {font_number} 0 R >> >>"
        )
        if content_number is not None:
            body += f" /Contents {content_number} 0 R"
        page_numbers.append(add((body + " >>").encode()))
    bodies[catalog_number] = f"<< /Type /Catalog /Pages {pages_number} 0 R >>".encode()
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    bodies[pages_number] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_numbers)} >>".encode()
    document = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(bodies):
        offsets[number] = len(document)
        document += f"{number} 0 obj\n".encode() + bodies[number] + b"\nendobj\n"
    xref_offset = len(document)
    highest = max(bodies)
    document += f"xref\n0 {highest + 1}\n".encode() + b"0000000000 65535 f \n"
    for number in range(1, highest + 1):
        document += f"{offsets[number]:010d} 00000 n \n".encode()
    document += (
        f"trailer\n<< /Size {highest + 1} /Root {catalog_number} 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(document)


def _parse(content: bytes, *, document_id: str = "doc") -> tuple[PDFPageInput, ...]:
    return parse_pdf_document(PDFDocumentInput(document_id=document_id, content=content))


def test_definition_declares_the_documented_identity() -> None:
    assert PDF_PAGE_SOURCE_DEFINITION.name == "pdf-page"
    assert PDF_PAGE_SOURCE_DEFINITION.version == "1"
    assert PDF_PAGE_SOURCE_DEFINITION.input_class is PDFPageInput
    assert PDF_PAGE_SOURCE_DEFINITION.source_class is PDFPageSource
    assert [projection.name for projection in PDF_PAGE_SOURCE_DEFINITION.projections] == [
        TEXT_EVIDENCE_PROJECTION_KEY.name
    ]


def test_every_physical_page_is_returned_in_order_with_its_digest() -> None:
    content = _pdf_document(["Net revenue 2022", None, "Restructuring costs"])
    pages = _parse(content)

    assert [page.page_number for page in pages] == [1, 2, 3]
    assert pages[0].text == "Net revenue 2022"
    assert pages[1].text == ""
    assert pages[2].text == "Restructuring costs"
    assert all(page.document_id == "doc" for page in pages)
    assert all(page.document_sha256 == hashlib.sha256(content).hexdigest() for page in pages)


def test_unreadable_documents_fail_without_partial_pages() -> None:
    for content in (b"", b"not a PDF", b"%PDF-1.7\ntruncated"):
        with pytest.raises(ValueError):
            _parse(content)


def test_blank_document_ids_are_rejected() -> None:
    content = _pdf_document(["Net revenue 2022"])
    with pytest.raises(ValueError):
        _parse(content, document_id="   ")
    with pytest.raises(ValueError):
        _parse(content, document_id="")


def test_empty_password_permission_markers_stay_readable() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.encrypt(user_password="", owner_password="owner")  # noqa: S106
    buffer = io.BytesIO()
    writer.write(buffer)

    pages = _parse(buffer.getvalue(), document_id="permissions")

    assert [(page.page_number, page.text) for page in pages] == [(1, "")]


def test_page_identity_is_stable_bounded_and_revision_specific() -> None:
    digest = hashlib.sha256(b"content").hexdigest()

    assert pdf_page_source_id("doc", digest, 3) == pdf_page_source_id("doc", digest, 3)
    assert pdf_page_source_id("doc", digest, 3) != pdf_page_source_id("doc", digest, 4)
    assert pdf_page_source_id("doc", digest, 3) != pdf_page_source_id("doc", digest + "a", 3)
    assert pdf_page_source_id("a:b", digest, 1) != pdf_page_source_id("a", digest + ":b", 1)
    assert len(pdf_page_source_id("d" * 400, digest, 1)) <= 256


def test_a_revised_document_produces_a_new_page_identity() -> None:
    original = _parse(_pdf_document(["Net revenue 2022"]), document_id="doc")
    revised = _parse(_pdf_document(["Net revenue 2023"]), document_id="doc")
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))

    original_source = asyncio.run(registry.resolve(original[0]))
    revised_source = asyncio.run(registry.resolve(revised[0]))

    assert original_source.name != revised_source.name


def test_captured_pages_persist_as_observations_with_one_identity() -> None:
    pages = _parse(_pdf_document(["Net revenue 2022", "Restructuring costs"]))
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))

    identities = []
    for page in pages:
        source = asyncio.run(registry.resolve(page))
        identities.append(source.source_id)
        assert source.name == source.source_id
        assert source.materialization.value == "captured"
        assert asyncio.run(registry.read(source)) == page

    assert len(set(identities)) == len(pages)
    assert identities[0] != identities[1]


def test_projection_exposes_exact_document_and_page_metadata() -> None:
    pages = _parse(_pdf_document(["Net revenue 2022"]))
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(pages[0]))

    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))

    assert evidence.source_type == "pdf-page"
    assert evidence.source_id == source.name
    assert evidence.content == "doc\nNet revenue 2022"
    assert evidence.metadata == {
        "document_id": "doc",
        "document_sha256": pages[0].document_sha256,
        "page_number": 1,
    }


def test_search_returns_distinct_page_citations(tmp_path: Path) -> None:
    content = _pdf_document([
        "Consolidated statement of income for fiscal 2022",
        "Restructuring costs of 91 million dollars",
        "Geographic net revenue by segment",
    ])
    pages = _parse(content)
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    observations = []
    for page in pages:
        source = asyncio.run(registry.resolve(page))
        observations.append(project_source_for_transport(registry, source))

    async def scenario() -> tuple[list[str], list[str]]:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pages.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope = await runtime.scopes.create(ScopeDraft(title="Pages", summary="Evidence", idempotency_key="pages"))
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
        async with open_builtin_runtime(config, candidate_pipeline=_EvidencePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            while (await memory.flush()).processed:
                pass
            citations = await search_pdf_pages(memory, "What were the restructuring costs?", limit=5)
            located = []
            for citation in citations:
                record = await memory.get(GetMemoryEntryRequest(citation=citation))
                located.append(record.entry.sources[0].source_id)
        return located, [observation.name for observation in observations]

    located, names = asyncio.run(scenario())

    assert len(located) == len(set(located))
    assert located == [names[1]]


async def _observe(registry: SourceDefinitionRegistry, source: PDFPageSource):
    from powercontext.sources import project_source_for_transport

    return project_source_for_transport(registry, source)


class _EvidencePipeline:
    """Turn each captured page into one Memory entry that keeps its Source."""

    async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(
                kind="source-evidence",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                sources=(source,),
            )
            for source in request.sources
            if source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"].strip()
        )
