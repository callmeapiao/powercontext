"""Parsing of engineering material into the records stored in seekdb.

Specifications are markdown files with a small YAML-like front matter block.
Python sources and tests are parsed with :mod:`ast`, so symbol names and
signatures come from the real grammar instead of a regex.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

FRONT_MATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*\n?(?P<rest>.*)$", re.DOTALL)


@dataclass
class SpecEntry:
    """One specification entry or design decision."""

    spec_id: str
    version: str
    title: str
    kind: str
    status: str
    supersedes: str | None
    scope: str | None
    body: str
    source_path: str

    @property
    def key(self) -> str:
        """Stable identity of this entry (``SPEC-002@v2``)."""

        return f"{self.spec_id}@{self.version}"


@dataclass
class CodeSymbol:
    """One function or class extracted from a Python source file."""

    path: str
    symbol: str
    kind: str
    signature: str
    body: str
    line_start: int


@dataclass
class TestCase:
    """One test function extracted from a test file."""

    path: str
    test_name: str
    kind: str
    body: str


@dataclass
class ParseResult:
    """Everything parsed out of one import run."""

    specs: list[SpecEntry] = field(default_factory=list)
    files: list[tuple[str, str, str]] = field(default_factory=list)
    symbols: list[CodeSymbol] = field(default_factory=list)
    tests: list[TestCase] = field(default_factory=list)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split a markdown file into its front matter mapping and its body."""

    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group("body").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('"').strip("'")
        meta[key.strip()] = value
    return meta, match.group("rest")


def _shown(path: Path, relative_to: Path | None) -> str:
    """Return ``path`` relative to ``relative_to`` when it is inside it."""

    if relative_to is None:
        return str(path).replace("\\", "/")
    try:
        return path.resolve().relative_to(Path(relative_to).resolve()).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def parse_spec_file(path: Path, relative_to: Path | None = None) -> SpecEntry:
    """Read one specification markdown file."""

    text = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(text)
    name = path.name
    default_id = name.split(".")[0]
    spec_id = meta.get("id") or default_id
    version = meta.get("version") or "1"
    title = meta.get("title") or spec_id
    kind = meta.get("kind") or "spec"
    status = meta.get("status") or "active"
    supersedes = meta.get("supersedes") or None
    scope = meta.get("scope") or None
    return SpecEntry(
        spec_id=spec_id,
        version=version,
        title=title,
        kind=kind,
        status=status,
        supersedes=supersedes,
        scope=scope,
        body=body.strip(),
        source_path=_shown(path, relative_to),
    )


def extract_python_symbols(path: Path, relative_to: Path | None = None) -> list[CodeSymbol]:
    """Extract top level functions and classes from a Python file."""

    source = path.read_text(encoding="utf-8")
    shown = _shown(path, relative_to)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    symbols: list[CodeSymbol] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        body = "\n".join(lines[node.lineno - 1 : end])
        if isinstance(node, ast.ClassDef):
            signature = f"class {node.name}"
        else:
            args = [a.arg for a in node.args.args]
            signature = f"{node.name}({', '.join(args)})"
        symbols.append(
            CodeSymbol(
                path=shown,
                symbol=node.name,
                kind=kind,
                signature=signature,
                body=body,
                line_start=node.lineno,
            )
        )
    return symbols


def extract_test_cases(path: Path, relative_to: Path | None = None) -> list[TestCase]:
    """Extract every ``test_*`` function from a test file."""

    source = path.read_text(encoding="utf-8")
    shown = _shown(path, relative_to)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    cases: list[TestCase] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test"):
            continue
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        body = "\n".join(lines[node.lineno - 1 : end])
        kind = "target" if "target" in node.name.lower() or "channel" in node.name.lower() else "regression"
        cases.append(TestCase(path=shown, test_name=node.name, kind=kind, body=body))
    return cases


def digest(text: str) -> str:
    """Return the SHA-256 digest of ``text``."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
