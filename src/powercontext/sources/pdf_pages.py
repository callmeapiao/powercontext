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

"""Page-level PDF capture with stable page identity and standard text evidence."""

from __future__ import annotations

import hashlib
import io
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import quote

from pydantic import BaseModel, model_validator

from powercontext.artifacts import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.runtime.models import (
        GetMemoryEntryRequest,
        MemoryEntryRecord,
        MemorySearchPage,
        SearchMemoryRequest,
    )

PDF_PAGE_SOURCE_NAME: Final = "pdf-page"
PDF_PAGE_SOURCE_VERSION: Final = "1"
PDF_PAGE_IDENTITY_PREFIX: Final = "pdf"
_SHA256_HEX_LENGTH: Final = 64
_MAX_PAGE_NUMBER_DIGITS: Final = 20
_MAX_DOCUMENT_TOKEN_LENGTH: Final = (
    MAX_SOURCE_ID_LENGTH - len(PDF_PAGE_IDENTITY_PREFIX) - 2 - _SHA256_HEX_LENGTH - _MAX_PAGE_NUMBER_DIGITS
)


class PDFDocumentInput(BaseModel):
    """One complete offline PDF document addressed by its document ID."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One captured physical page that keeps its document identity and page number."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A captured PDF page that retrieval citations can point back to."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="after")
    def validate_identity(self) -> PDFPageSource:
        expected = pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        if self.source_id != expected or self.name != expected:
            raise ValueError("PDF page identity does not match its document, digest and page number")  # noqa: TRY003
        return self


class ScopedPageMemory(Protocol):
    """The scoped Memory surface used to retrieve page citations."""

    async def search(self, request: SearchMemoryRequest, /) -> MemorySearchPage: ...

    async def get(self, request: GetMemoryEntryRequest, /) -> MemoryEntryRecord: ...


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the stable identity shared by one captured page and its citations.

    The identity is decided by the document ID, the digest of the whole document
    and the physical page number, so a revised PDF produces a new identity while
    older citations keep pointing at the revision they were captured from.
    """
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if not isinstance(document_sha256, str) or not document_sha256:
        raise ValueError("document_sha256 must be a non-empty string")  # noqa: TRY003
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")  # noqa: TRY003
    source_id = f"{PDF_PAGE_IDENTITY_PREFIX}:{_document_token(document_id)}:{document_sha256}:{page_number}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse one complete PDF document into one validated input per physical page.

    Every physical page is parsed before any page is returned, so an unreadable
    document fails atomically instead of yielding partial pages. Pages without
    extractable text are kept as empty pages to preserve the page boundaries.
    """
    document_id = value.document_id
    if not document_id.strip():
        raise ValueError("document_id must not be empty or whitespace")  # noqa: TRY003
    content = value.content
    if not content:
        raise ValueError("PDF content must not be empty")  # noqa: TRY003
    document_sha256 = hashlib.sha256(content).hexdigest()
    return tuple(
        PDFPageInput(
            document_id=document_id,
            document_sha256=document_sha256,
            page_number=page_number,
            text=text,
        )
        for page_number, text in enumerate(_extract_page_texts(content), start=1)
    )


async def search_pdf_pages(memory: ScopedPageMemory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return the exact page citations that answer one question.

    The search runs on the scoped Memory through its public search API, so every
    citation resolves to a stored page. Repeated pages are skipped, and at most
    ``limit`` distinct pages are returned.
    """
    # The Runtime request models are imported here so this module keeps no import
    # edge from the Source layer back into the Runtime that owns the Memory.
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

    if limit < 1:
        return ()
    page = await memory.search(
        SearchMemoryRequest(query=question, mode="fts", limit=max(2 * limit, limit + 1)),
    )
    citations: list[MemoryCitation] = []
    seen: set[tuple[str, str]] = set()
    for hit in page.hits:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        record = await memory.get(GetMemoryEntryRequest(citation=citation))
        references = record.entry.sources
        if len(references) != 1:
            # One citation must name exactly one page, otherwise it cannot be
            # read back as a single evidence page.
            continue
        identity = (references[0].source_type, references[0].source_id)
        if identity in seen:
            continue
        seen.add(identity)
        citations.append(citation)
        if len(citations) == limit:
            break
    return tuple(citations)


class PDFPageAdapter:
    """Resolve and read one captured PDF page."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one captured PDF page without coupling consumers to ``PDFPageSource``."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_SOURCE_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


def _document_token(document_id: str) -> str:
    token = quote(document_id, safe="")
    if len(token) <= _MAX_DOCUMENT_TOKEN_LENGTH:
        return token
    # Long document IDs would push the identity past its bound; a digest of the
    # same value keeps the identity deterministic and free of collisions.
    return hashlib.sha256(document_id.encode("utf-8")).hexdigest()


def _extract_page_texts(content: bytes) -> tuple[str, ...]:
    """Extract the readable text of every physical page in one PDF document."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        return tuple((page.extract_text() or "").strip() for page in reader.pages)
    except Exception as error:
        # pypdf reports empty, truncated, encrypted and structurally broken
        # documents with unrelated exception types; all of them are document
        # failures that must not surface as partial pages.
        raise ValueError("PDF content must be a complete readable PDF document") from error  # noqa: TRY003


__all__ = [
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDF_PAGE_SOURCE_VERSION",
    "PDFDocumentInput",
    "PDFPageAdapter",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageTextEvidenceProjection",
    "ScopedPageMemory",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
