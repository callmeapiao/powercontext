"""Helpers for turning a model answer into a file change and a diff."""

from __future__ import annotations

import difflib
import re
from pathlib import Path

FENCE_RE = re.compile(r"```(?P<lang>[A-Za-z0-9_+-]*)\s*\n(?P<body>.*?)\n?```", re.DOTALL)


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Return every fenced code block in ``text`` as ``(language, body)``."""

    return [(m.group("lang").lower(), m.group("body")) for m in FENCE_RE.finditer(text)]


def extract_code_block(text: str, *, prefer: str | None = None) -> str:
    """Pick the code block that carries the replacement file.

    ``prefer`` selects the first block whose body contains that marker; without
    it the last block is used, because models usually put the final answer
    last.
    """

    blocks = extract_code_blocks(text)
    if not blocks:
        raise ValueError("the assistant answer contains no fenced code block")
    if prefer:
        for _lang, body in blocks:
            if prefer in body:
                return body
    return blocks[-1][1]


def unified_diff(path: str, before: str, after: str) -> str:
    """Return a unified diff between two file versions."""

    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def write_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` keeping the file's line ending style."""

    path.parent.mkdir(parents=True, exist_ok=True)
    normalised = content.replace("\r\n", "\n")
    if normalised and not normalised.endswith("\n"):
        normalised += "\n"
    path.write_text(normalised, encoding="utf-8", newline="\n")
