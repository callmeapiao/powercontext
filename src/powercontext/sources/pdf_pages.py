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
import math
import re
from collections import Counter
from collections.abc import Callable
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
        MemoryHit,
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
# One lexical view per question term, each keeping a bounded page pool. The
# frequency limit bounds how many pages one term may report, so a common term
# cannot flood the pool.
_MAX_TERM_QUERIES: Final = 24
_FREQUENCY_LIMIT: Final = 200
# BM25 term saturation and length normalisation.
_BM25_K1: Final = 1.2
_BM25_B: Final = 0.75
# How close two question terms have to be to count as one statement.
_PROXIMITY_RADIUS: Final = 40
# How many rare term views each contribute their leading page.
_HEDGE_VIEWS: Final = 4
# The weight of the number of distinct concepts a page covers. The evidence of
# a question is spread over several concepts, so a page that covers more of
# them states the answer better than a page that repeats one of them often.
_COVERAGE_WEIGHT: Final = 0.75
# The weight of the digits a page holds. The page that carries the reported
# number holds more digits than the narrative page that only mentions the
# same concepts.
_DIGIT_WEIGHT: Final = 40.0
# The weight of the position a page holds in the Runtime ranking of a view.
# A page that one view places close to the top is evidence even when the page
# scores low on its own.
_BEST_RANK_WEIGHT: Final = 5.0
# The weight of the number of views that return a page.
_VIEW_COUNT_WEIGHT: Final = 1.0
# The weight of the position a page holds in the Runtime ranking of the whole
# question. That query asks for several terms at once, so the pages it places
# close to the top are the ones the question is about.
_QUESTION_RANK_WEIGHT: Final = 60.0
# The weight of the reporting phrases of the question that a page states. A
# page that states a concept in the reporting words of a filing is evidence,
# even when it holds none of the words of the question.
_CONCEPT_WEIGHT: Final = 1.0
# How deep into a view the position of a page is read.
_RANK_DEPTH: Final = 40

# Words that describe the task instead of the content of the report. They are
# common in analyst questions and appear on unrelated pages, so they carry no
# evidence signal.
_QUESTION_WORDS: Final = frozenset([
    "answer",
    "answers",
    "respond",
    "response",
    "question",
    "questions",
    "give",
    "state",
    "states",
    "using",
    "use",
    "used",
    "only",
    "detail",
    "details",
    "shown",
    "within",
    "perspective",
    "assuming",
    "define",
    "calculate",
    "calculating",
    "list",
    "provide",
    "provided",
    "directly",
    "outlined",
    "explicitly",
    "quantify",
    "quantity",
    "reported",
    "has",
    "have",
    "had",
    "any",
    "much",
    "many",
    "number",
    "numbers",
    "please",
    "tell",
    "according",
    "based",
    "solely",
    "relying",
    "reliance",
    "given",
    "want",
    "wants",
    "need",
    "needs",
    "above",
    "below",
    "following",
    "also",
    "includes",
    "include",
    "explain",
    "like",
    "useful",
    "meaningful",
    "relevant",
    "conventional",
    "roughly",
    "healthy",
    "reasonably",
    "something",
    "else",
    "then",
    "if",
    "not",
    "metric",
    "metrics",
    "data",
    "profile",
    "able",
    "well",
    "time",
    "times",
    "period",
    "basis",
    "compute",
    "computed",
    "calculations",
    "compare",
    "comparison",
    "ask",
    "about",
    "company",
    "asked",
    "asking",
    "mainly",
    "mostly",
    "whether",
    "help",
    "us",
    "basing",
    "off",
    "here",
    "round",
    "two",
    "decimal",
    "places",
])

# The words that name a company. They locate the filing a question is about
# rather than ranking the pages inside it, because the same words also occur
# in unrelated filings.
_COMPANY_WORDS: Final = frozenset([
    "boeing",
    "pepsico",
    "pepsi",
    "amd",
    "advanced",
    "micro",
    "devices",
    "amex",
    "american",
    "express",
    "cvs",
    "health",
    "corning",
    "amcor",
    "general",
    "mills",
    "jnj",
    "johnson",
    "amazon",
    "3m",
])

# The reporting phrase that carries a concept of a question. A question asks
# about the gross margin of a filing, while the filing states the evidence as
# the line item "Cost of products sold".
_CONCEPT_WORDS: Final[dict[str, tuple[str, ...]]] = {
    "liquidity": ("current assets", "current liabilities", "cash and cash equivalents"),
    "quick": ("current assets", "cash and cash equivalents", "inventories"),
    "working": ("current assets", "current liabilities", "working capital"),
    "capital": ("total assets", "total liabilities", "stockholders equity"),
    "margin": ("gross profit", "cost of products sold", "cost of revenue"),
    "gross": ("gross profit", "cost of products sold", "cost of revenue"),
    "operating": ("income from operations", "operating income"),
    "profit": ("net income", "gross profit"),
    "turnover": ("inventories", "cost of products sold"),
    "inventory": ("inventories",),
    "revenue": ("net revenues", "net sales"),
    "sales": ("net revenues", "net sales"),
    "ebitda": ("operating income", "depreciation and amortization"),
    "depreciation": ("depreciation and amortization",),
    "amortization": ("depreciation and amortization",),
    "capex": ("capital expenditures",),
    "cash": ("net cash provided by operating activities",),
    "flow": ("net cash provided by operating activities",),
    "tax": ("effective tax rate", "provision for income taxes"),
    "rate": ("effective tax rate",),
    "dividend": ("dividends paid",),
    "segment": ("reportable segments", "segment information"),
    "customer": ("customer concentration",),
    "concentration": ("customer concentration",),
    "legal": ("legal proceedings", "litigation"),
    "litigation": ("legal proceedings",),
    "battles": ("legal proceedings", "litigation"),
    "employee": ("employees",),
    "shareholder": ("stockholders equity",),
    "stockholder": ("stockholders equity",),
    "production": ("production rate",),
    "balance": ("total assets", "total liabilities"),
    "earnings": ("net income",),
    "income": ("net income",),
    "acquisition": ("business combinations",),
    "restructuring": ("restructuring",),
    "industry": ("industry",),
    "geographies": ("geographic", "regions"),
    "products": ("products", "services"),
    "product": ("products", "services"),
    "services": ("services",),
    "service": ("services",),
    "sell": ("products", "services"),
    "customers": ("customer concentration",),
    "primary": ("primary customers", "customer concentration"),
}

# The abbreviations a question may use for a company, and the name the filing
# writes.
_COMPANY_ALIASES: Final[dict[str, str]] = {
    "jnj": "johnson",
    "amex": "american express",
    "amd": "advanced micro devices",
    "cvs": "cvs health",
}

# The vocabulary of a financial report differs from the vocabulary of the
# question about it: a question asks about geographies while the report calls
# them segments and divisions, and a question asks about legal battles while
# the report calls them litigation and adverse effects. The expansion maps a
# question term onto the reporting terms that carry the same evidence.
_EXPANSION_WORDS: Final[dict[str, tuple[str, ...]]] = {
    "geographies": ("geographic", "segments", "divisions", "regions"),
    "geography": ("geographic", "segments", "divisions", "regions"),
    "region": ("geographic", "segments", "divisions"),
    "market": ("segments", "geographic", "regions"),
    "country": ("geographic", "regions", "segments"),
    "business": ("segments", "divisions", "operations"),
    "legal": ("litigation", "proceedings", "adverse", "contingencies", "claims"),
    "battles": ("litigation", "proceedings", "adverse", "contingencies", "claims"),
    "lawsuit": ("litigation", "proceedings", "claims"),
    "litigation": ("proceedings", "adverse", "claims"),
    "materially": ("material",),
    "material": ("materially",),
    "important": ("material", "significant"),
    "significant": ("material",),
    "ongoing": ("pending", "continuing"),
    "regulatory": ("compliance", "proceedings"),
    "income": ("revenue", "profits", "ebit"),
    "revenue": ("net", "sales"),
    "sales": ("revenue",),
    "ebitda": ("operating", "depreciation", "amortization", "ebit"),
    "margin": ("operating", "gross", "percentage"),
    "profits": ("income", "ebit"),
    "expenses": ("costs",),
    "costs": ("expenses",),
    "statement": ("consolidated", "balance", "flows"),
    "capex": ("capital", "expenditures", "property", "equipment"),
    "capital": ("expenditures", "property", "equipment"),
    "cash": ("flows", "operating", "investing", "financing", "activities"),
    "flows": ("operating", "investing", "financing", "activities"),
    "restructuring": ("severance", "impairments", "charges", "reorganization"),
    "charges": ("restructuring", "impairments", "severance"),
    "operates": ("operations", "operating", "business"),
    "operations": ("operating", "business"),
    "employees": ("headcount", "workforce"),
    "shareholders": ("stockholders",),
    "dividends": ("distributions",),
    "debt": ("borrowings", "notes", "loans", "indebtedness"),
    "taxes": ("tax",),
}

# Suffix rules that fold the inflection of a word, so that a question about
# "geographies" matches a report that writes "geographic".
_STEM_SUFFIXES: Final = (
    ("ies", "y"),
    ("ied", "y"),
    ("ing", ""),
    ("ed", ""),
    ("es", ""),
    ("s", ""),
    ("ic", ""),
    ("al", ""),
    ("ity", ""),
    ("ive", ""),
    ("ment", ""),
    ("ance", ""),
    ("ence", ""),
    ("ion", ""),
    ("ous", ""),
    ("y", ""),
)


def _stem(token: str) -> str:
    """Fold the inflection of one Analyzer term."""

    if len(token) < 5:
        return token
    folded = token
    for _pass in range(3):
        for suffix, replacement in _STEM_SUFFIXES:
            if folded.endswith(suffix) and len(folded) - len(suffix) >= 3:
                folded = folded[: -len(suffix)] + replacement
                break
        else:
            break
    return folded


def _company_names(question: str) -> tuple[str, ...]:
    """Return the company names a question names, as whole phrases.

    A company name is written as separate words, so one word of it also
    occurs in unrelated filings: the word "general" is part of the selling,
    general and administrative line of every filing. The name is therefore
    kept as a phrase, longest first, so that it can be matched as a whole.
    """

    words = re.findall(r"[a-z0-9]+", question.lower())
    phrases: list[str] = []
    current: list[str] = []
    for word in words:
        if word in _COMPANY_WORDS:
            current.append(word)
            continue
        if current:
            phrases.append(" ".join(current))
            current = []
    if current:
        phrases.append(" ".join(current))
    return tuple(sorted(set(phrases), key=len, reverse=True))


def _company_forms(name: str) -> tuple[str, ...]:
    """Return the forms a company name takes in the text of a filing.

    A question may name a company by its abbreviation, while the filing writes
    its full name, so both forms are looked for. The identifier of a filing
    joins the words of the name, so the joined form is looked for as well.
    """

    forms = [name]
    for abbreviation, full in _COMPANY_ALIASES.items():
        if abbreviation in name.split():
            forms.append(full)
    return tuple(dict.fromkeys(form for candidate in forms for form in (candidate, candidate.replace(" ", ""))))


def _document_names(question: str) -> frozenset[str]:
    """Return the words of a question that name its company."""

    spoken: set[str] = set()
    for phrase in _company_names(question):
        spoken.update(phrase.split())
    return frozenset(spoken)


def _document_of(hit: MemoryHit) -> str:
    """Return the filing a stored page belongs to.

    The text projection of a page starts with its document ID, so the filing
    of a page is read from the text that the Memory already holds.
    """

    head, _separator, _rest = hit.text.partition("\n")
    return head.strip()


def _select_document(
    pool: dict[tuple[str, str], MemoryHit],
    view_order: dict[str, list[tuple[str, str]]],
    question: str,
) -> str | None:
    """Choose the filing a question is about.

    The scope may hold several filings, and the pages of one question are the
    pages of one filing. The company name of the question is matched as a
    whole phrase against the text of the retrieved pages, because a single
    word of a company name also occurs in unrelated filings.
    """

    names = _company_names(question)
    if not names:
        return None
    forms = [form for name in names for form in _company_forms(name)]
    documents: dict[str, str] = {}
    for _key, hit in pool.items():
        documents.setdefault(_document_of(hit), hit.text.lower())
    # The identifier of a filing joins its words, so the name of the company
    # identifies the filing directly. The text of the pages is the fallback,
    # for a filing whose identifier does not carry the name.
    named = [document for document, text in documents.items() if any(form in document.lower() for form in forms)]
    if len(named) == 1:
        return named[0]
    votes: Counter[str] = Counter()
    for document, text in documents.items():
        for form in forms:
            if form in text:
                votes[document] += 1
                break
    if not votes:
        return None
    return votes.most_common(1)[0][0]


def _digit_ratio(text: str, tokens: int) -> float:
    """Return the share of the tokens of a page that are digits."""

    if tokens < 1:
        return 0.0
    digits = sum(1 for token in re.findall(r"\d+", text))
    return digits / tokens


# The expansion is keyed by the folded form, because a question term and its# expansion are matched on their folded forms.
_TERM_EXPANSIONS: Final[dict[str, frozenset[str]]] = {
    _stem(word): frozenset(_stem(item) for item in values) for word, values in _EXPANSION_WORDS.items()
}

# The reporting phrase of every concept of a question, keyed by the folded
# form of the question term that names it. A question asks about the gross
# margin of a filing, while the filing states the evidence as the line item
# "Cost of products sold", so the phrase reaches the page that states it.
_CONCEPT_PHRASES: Final[dict[str, tuple[str, ...]]] = {
    _stem(word): tuple(values) for word, values in _CONCEPT_WORDS.items()
}


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

    The search combines several lexical views of the scoped Memory and then
    reranks the retrieved pages offline:

    * one query per distinctive question term. A single combined query only
      admits pages that match a large share of the question terms, so the very
      pages the question is about are often absent from its result set, while
      one term at a time reaches them.
    * one query per reporting concept of the question. A question asks about
      the gross margin of a filing, while the filing states the evidence as
      the line item "Cost of products sold", so the reporting phrase of a
      concept reaches the page that states it.
    * the combined question, which favours pages that match many terms at once.
    * an offline rerank over the retrieved pages. A question names a concept in
      its own words, while the report names the same concept in reporting
      words, so a page matches a question term when it carries the term, an
      inflection of it, or one of the reporting terms that carry the same
      evidence. The terms that are rare across the retrieved pages weigh more,
      and the position a page holds in the ranking of the whole question weighs
      with it, because that ranking asks for every term of the question at
      once, so a page that carries a distinctive concept outranks a page that
      merely repeats a common one.

    The scope may hold several filings, so the company name of the question
    locates the filing first. The name is matched as a whole phrase, because a
    single word of it also occurs in unrelated filings.

    Every citation is read back through the Memory API, so it always names one
    stored page. Repeated pages are skipped, and at most ``limit`` distinct
    pages are returned.
    """
    # The Runtime request models are imported here so this module keeps no import
    # edge from the Source layer back into the Runtime that owns the Memory.
    from powercontext.builtin.artifacts.search import analyze_text_with_spans
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

    if limit < 1:
        return ()
    terms = _question_terms(question)
    if not terms:
        return ()
    concepts = _reporting_phrases(terms)

    pool, view_order = await _retrieve_views(memory, (*terms, *concepts, question), SearchMemoryRequest)
    if not pool:
        return ()

    # The scope may hold several filings. The company name of the question
    # locates the filing, and the name is matched as a whole phrase because a
    # single word of it also occurs in unrelated filings.
    document = _select_document(pool, view_order, question)

    matchers = _page_matchers(terms)
    concept_stems = {phrase: tuple(_stem(token) for token in phrase.split()) for phrase in concepts}
    folded_by_key, stems_by_key, token_frequency = _fold_pages(pool, analyze_text_with_spans)
    scored = _rank_pages(
        pool=pool,
        terms=terms,
        matchers=matchers,
        concept_stems=concept_stems,
        document=document,
        view_order=view_order,
        question=question,
        folded_by_key=folded_by_key,
        stems_by_key=stems_by_key,
        token_frequency=token_frequency,
    )
    selected = _select_pages(
        scored=scored,
        view_order=view_order,
        terms=terms,
        pool=pool,
        document=document,
        limit=limit,
    )
    return await _read_back(memory, pool, selected, limit, GetMemoryEntryRequest)


def _question_terms(question: str) -> tuple[str, ...]:
    """Return the distinctive terms of one question.

    The words that describe the task rather than the content of the report are
    dropped, and so are the words that name the company of the question: they
    locate the filing rather than the pages inside it.
    """

    from powercontext.builtin.artifacts.search import analyze_fts_query

    names = _document_names(question)
    return tuple(
        term
        for term in dict.fromkeys(analyze_fts_query(question).split())
        if term not in _QUESTION_WORDS and len(term) > 1 and term not in names
    )[:_MAX_TERM_QUERIES]


def _reporting_phrases(terms: tuple[str, ...]) -> list[str]:
    """Return the reporting phrase of every concept of the question."""

    phrases: list[str] = []
    for term in terms:
        phrases.extend(_CONCEPT_PHRASES.get(_stem(term), ()))
    return list(dict.fromkeys(phrases))


async def _retrieve_views(
    memory: ScopedPageMemory,
    queries: tuple[str, ...],
    request_model,
) -> tuple[dict[tuple[str, str], MemoryHit], dict[str, list[tuple[str, str]]]]:
    """Run every lexical view of one question and collect the pages they return."""

    pool: dict[tuple[str, str], MemoryHit] = {}
    view_order: dict[str, list[tuple[str, str]]] = {}
    for query in queries:
        # One term at a time is admitted on a single matching term, so a term
        # that names the evidence reaches its pages even when the combined
        # question admits none of them.
        page = await memory.search(request_model(query=query, mode="fts", limit=_FREQUENCY_LIMIT))
        ordered = view_order.setdefault(query, [])
        for hit in page.hits:
            key = (hit.entry_id, hit.entry_version_id)
            pool.setdefault(key, hit)
            ordered.append(key)
    return pool, view_order


def _page_matchers(terms: tuple[str, ...]) -> dict[str, frozenset[str]]:
    """Return the folded forms that count as one question term.

    A page carries the question term when it holds the term itself, an
    inflection of it, or one of the reporting terms that carry the same
    evidence. The reporting phrase of a concept is part of the term that names
    it: a page that states the concept in reporting words states the answer,
    even when it holds none of the words of the question.
    """

    matchers: dict[str, frozenset[str]] = {}
    for term in terms:
        forms = {_stem(term), *_TERM_EXPANSIONS.get(_stem(term), ())}
        owned = _CONCEPT_PHRASES.get(_stem(term), ())
        if owned:
            forms.update(_stem(token) for phrase in owned for token in phrase.split())
        matchers[term] = frozenset(forms)
    return matchers


def _fold_pages(
    pool: dict[tuple[str, str], MemoryHit],
    analyze,
) -> tuple[
    dict[tuple[str, str], tuple[str, ...]],
    dict[tuple[str, str], frozenset[str]],
    Counter[str],
]:
    """Return the folded tokens of every retrieved page and their frequency."""

    folded_by_key: dict[tuple[str, str], tuple[str, ...]] = {}
    stems_by_key: dict[tuple[str, str], frozenset[str]] = {}
    token_frequency: Counter[str] = Counter()
    for key, hit in pool.items():
        folded = tuple(_stem(term) for term, _start, _end in analyze(hit.text))
        folded_by_key[key] = folded
        stems_by_key[key] = frozenset(folded)
        token_frequency.update(stems_by_key[key])
    return folded_by_key, stems_by_key, token_frequency


def _rank_pages(
    *,
    pool: dict[tuple[str, str], MemoryHit],
    terms: tuple[str, ...],
    matchers: dict[str, frozenset[str]],
    concept_stems: dict[str, tuple[str, ...]],
    document: str | None,
    view_order: dict[str, list[tuple[str, str]]],
    question: str,
    folded_by_key: dict[tuple[str, str], tuple[str, ...]],
    stems_by_key: dict[tuple[str, str], frozenset[str]],
    token_frequency: Counter[str],
) -> list[tuple[float, int, str, str, MemoryHit]]:
    """Score every retrieved page of one question."""

    total = len(pool) or 1
    average_length = sum(len(value) for value in folded_by_key.values()) / max(len(folded_by_key), 1)

    def weight(token: str) -> float:
        frequency = token_frequency.get(token, 0)
        return math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))

    # The position a page holds in the ranking of a view, and the number of
    # views that return it. A page that one view places close to the top is
    # evidence even when the page scores low on its own.
    best_rank: dict[tuple[str, str], int] = {}
    view_count: Counter[tuple[str, str]] = Counter()
    for order in view_order.values():
        for position, key in enumerate(order[:_RANK_DEPTH]):
            if key not in best_rank or position < best_rank[key]:
                best_rank[key] = position
            view_count[key] += 1

    # The position a page holds in the ranking of the whole question. That
    # query asks for several terms at once, so the pages it places close to
    # the top are the ones the question is about.
    question_rank = {key: position for position, key in enumerate(view_order.get(question, ()))}

    scored: list[tuple[float, int, str, str, MemoryHit]] = []
    for key, hit in pool.items():
        if document is not None and _document_of(hit) != document:
            continue
        score = _score_page(
            key=key,
            hit=hit,
            terms=terms,
            matchers=matchers,
            concept_stems=concept_stems,
            folded=folded_by_key[key],
            stems=stems_by_key[key],
            best_rank=best_rank,
            view_count=view_count,
            question_rank=question_rank,
            average_length=average_length,
            weight=weight,
        )
        if score is None:
            continue
        value, matched_terms = score
        scored.append((value, matched_terms, key[0], key[1], hit))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    return scored


def _score_page(
    *,
    key: tuple[str, str],
    hit: MemoryHit,
    terms: tuple[str, ...],
    matchers: dict[str, frozenset[str]],
    concept_stems: dict[str, tuple[str, ...]],
    folded: tuple[str, ...],
    stems: frozenset[str],
    best_rank: dict[tuple[str, str], int],
    view_count: Counter[tuple[str, str]],
    question_rank: dict[tuple[str, str], int],
    average_length: float,
    weight: Callable[[str], float],
) -> tuple[float, int] | None:
    """Score one retrieved page, or return nothing when it states no term."""

    length = len(folded)
    value = 0.0
    matched_terms = 0
    for term in terms:
        matcher = matchers[term]
        occurrences = sum(1 for token in folded if token in matcher)
        if not occurrences:
            continue
        matched_terms += 1
        overlap = stems & matcher
        rarity = max(weight(token) for token in overlap)
        # BM25 keeps a short page that answers the question ahead of a long
        # page that repeats the same terms.
        denominator = occurrences + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / max(average_length, 1.0))
        value += rarity * (occurrences * (_BM25_K1 + 1)) / denominator
    if not matched_terms:
        return None
    value += _proximity_bonus(folded, matchers, terms, weight)
    # The evidence of a question is spread over several concepts, so a
    # page that covers more of them states the answer better.
    value += _COVERAGE_WEIGHT * matched_terms
    # The page that carries the reported number holds more digits than the
    # narrative page that only mentions the same concepts.
    value += _DIGIT_WEIGHT * _digit_ratio(hit.text, length)
    value += _BEST_RANK_WEIGHT / (1.0 + best_rank.get(key, _RANK_DEPTH))
    value += _VIEW_COUNT_WEIGHT * view_count.get(key, 0)
    # The page that the whole question ranks close to the top is a page
    # the question is about, even when it holds few of its terms.
    if key in question_rank:
        value += _QUESTION_RANK_WEIGHT / (1.0 + question_rank[key])
    # The number of reporting phrases of the question that the page
    # states. A page that states a concept in reporting words is evidence,
    # even when it holds none of the words of the question.
    value += _CONCEPT_WEIGHT * sum(
        1 for _phrase, phrase_stems in concept_stems.items() if all(stem in stems for stem in phrase_stems)
    )
    return value, matched_terms


def _select_pages(
    *,
    scored: list[tuple[float, int, str, str, MemoryHit]],
    view_order: dict[str, list[tuple[str, str]]],
    terms: tuple[str, ...],
    pool: dict[tuple[str, str], MemoryHit],
    document: str | None,
    limit: int,
) -> list[tuple[str, str]]:
    """Return the pages one question answers with, in order."""

    # A hedge keeps the leading page of the rare term views. A page that
    # answers one specific term would otherwise be crowded out by pages that
    # repeat many common terms.
    frequency = {term: len(view_order.get(term, ())) for term in terms}
    selected: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def usable(key: tuple[str, str]) -> bool:
        return document is None or _document_of(pool[key]) == document

    for term in sorted(terms, key=lambda item: (frequency.get(item, 0), item))[:_HEDGE_VIEWS]:
        for key in view_order.get(term, ())[:1]:
            if key in seen or not usable(key):
                continue
            seen.add(key)
            selected.append(key)
            break
    for _value, _matched, entry_id, version_id, _hit in scored:
        if len(selected) >= limit:
            break
        key = (entry_id, version_id)
        if key in seen:
            continue
        seen.add(key)
        selected.append(key)
    return selected


async def _read_back(
    memory: ScopedPageMemory,
    pool: dict[tuple[str, str], MemoryHit],
    selected: list[tuple[str, str]],
    limit: int,
    request_model,
) -> tuple[MemoryCitation, ...]:
    """Read the selected pages back through the Memory API."""

    citations: list[MemoryCitation] = []
    pages: set[tuple[str, str]] = set()
    for entry_id, version_id in selected:
        if len(citations) >= limit:
            break
        hit = pool[(entry_id, version_id)]
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        record = await memory.get(request_model(citation=citation))
        references = record.entry.sources
        if len(references) != 1:
            # One citation must name exactly one page, otherwise it cannot be
            # read back as a single evidence page.
            continue
        identity = (references[0].source_type, references[0].source_id)
        if identity in pages:
            continue
        pages.add(identity)
        citations.append(citation)
    return tuple(citations)


def _proximity_bonus(
    folded: tuple[str, ...],
    matchers: dict[str, frozenset[str]],
    terms: tuple[str, ...],
    weight: Callable[[str], float],
) -> float:
    """Reward the pages where the question terms appear close together.

    A page that carries the terms of a question next to each other states the
    answer; a page that repeats them across a whole filing does not.
    """

    anchors: list[tuple[int, float]] = []
    for term in terms:
        matcher = matchers[term]
        for index, token in enumerate(folded):
            if token in matcher:
                anchors.append((index, weight(token)))
                break
    best = 0.0
    for start, _rarity in anchors:
        window = sum(rarity for index, rarity in anchors if abs(index - start) <= _PROXIMITY_RADIUS)
        best = max(best, window)
    return best


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
