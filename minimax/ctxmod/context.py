"""Assembly of the development context for one task.

The context is built from three places and nowhere else:

* the **specifications, code and tests** stored in seekdb,
* the **task state** persisted by PowerContext,
* the **task description** supplied by the caller.

Nothing about the expected answer is hard-coded: a specification that is
superseded is dropped from the applicable set and reported as invalidated,
and a specification that is replaced by a newer import immediately changes
the context that is produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .state import TaskState

CJK_RE = re.compile(r"[一-鿿]+")
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def extract_keywords(text: str, limit: int = 12) -> list[str]:
    """Derive retrieval keywords from a task description.

    ASCII words are taken as-is; CJK runs are expanded into character bigrams
    so that a short Chinese description still matches the stored material.
    """

    keywords: list[str] = []
    for word in WORD_RE.findall(text):
        lowered = word.lower()
        if lowered not in keywords:
            keywords.append(lowered)
    for run in CJK_RE.findall(text):
        if len(run) == 1:
            if run not in keywords:
                keywords.append(run)
            continue
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram not in keywords:
                keywords.append(bigram)
    return keywords[:limit]


@dataclass
class SpecView:
    """One specification entry as it appears in the context."""

    key: str
    spec_id: str
    version: str
    title: str
    kind: str
    status: str
    scope: str | None
    body: str
    source_path: str
    superseded_by: str | None = None


@dataclass
class DevelopmentContext:
    """The complete context handed to the development assistant."""

    task_id: str
    description: str
    keywords: list[str] = field(default_factory=list)
    applicable_specs: list[SpecView] = field(default_factory=list)
    invalidated_specs: list[SpecView] = field(default_factory=list)
    code_files: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    state: TaskState | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "keywords": self.keywords,
            "applicable_specs": [s.__dict__ for s in self.applicable_specs],
            "invalidated_specs": [s.__dict__ for s in self.invalidated_specs],
            "code_files": self.code_files,
            "symbols": self.symbols,
            "tests": self.tests,
            "state": self.state.model_dump() if self.state else None,
        }

    def render(self) -> str:
        """Render the context as the prompt body sent to the assistant."""

        lines: list[str] = []
        lines.append("=== 任务描述 ===")
        lines.append(self.description.strip())
        lines.append("")
        lines.append("=== 当前适用的规格条目（必须遵守） ===")
        if self.applicable_specs:
            for spec in self.applicable_specs:
                lines.append(f"--- {spec.key} | {spec.title} | kind={spec.kind} | source={spec.source_path}")
                if spec.scope:
                    lines.append(f"scope: {spec.scope}")
                lines.append(spec.body.strip())
                lines.append("")
        else:
            lines.append("(无)")
            lines.append("")
        lines.append("=== 已失效的决策（不要遵循） ===")
        if self.invalidated_specs:
            for spec in self.invalidated_specs:
                reason = f"已被 {spec.superseded_by} 取代" if spec.superseded_by else "已作废"
                lines.append(f"--- {spec.key} | {spec.title} | {reason} | source={spec.source_path}")
                lines.append(spec.body.strip())
                lines.append("")
        else:
            lines.append("(无)")
            lines.append("")
        lines.append("=== 需要修改的代码文件 ===")
        for item in self.code_files:
            lines.append(f"--- {item['path']}")
            lines.append(item["content"].rstrip())
            lines.append("")
        lines.append("=== 相关符号 ===")
        for item in self.symbols:
            lines.append(f"--- {item['path']} :: {item['symbol']} ({item['kind']}) line {item['line_start']}")
            lines.append(item["body"].rstrip())
            lines.append("")
        lines.append("=== 用于验证的测试 ===")
        for item in self.tests:
            lines.append(f"--- {item['path']} :: {item['test_name']} ({item['kind']})")
            lines.append(item["body"].rstrip())
            lines.append("")
        if self.state is not None:
            lines.append("=== 已有任务状态（PowerContext） ===")
            lines.append(f"已完成: {self.state.done or '（无）'}")
            lines.append(f"待办: {self.state.todo or '（无）'}")
            if self.state.validations:
                lines.append("已有验证结果:")
                for record in self.state.validations:
                    lines.append(
                        f"  - {record.command} -> exit={record.exit_code} "
                        f"(passed={record.passed}, failed={record.failed}) {record.summary}"
                    )
            else:
                lines.append("已有验证结果: （无）")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _normalise_key(reference: str) -> str:
    """Normalise a ``SPEC-002@v1`` reference into the stored ``SPEC-002@1`` form."""

    if "@" not in reference:
        return f"{reference}@1"
    spec_id, _, version = reference.partition("@")
    return f"{spec_id}@{version.lstrip('v') or '1'}"


def resolve_spec_status(specs: list[dict[str, Any]]) -> tuple[list[SpecView], list[SpecView]]:
    """Split stored specifications into applicable and invalidated entries.

    A specification is invalidated when its own ``status`` says so, or when
    another imported entry declares that it supersedes it.
    """

    by_key: dict[str, SpecView] = {}
    for row in specs:
        key = f"{row['spec_id']}@{row['version']}"
        by_key[key] = SpecView(
            key=key,
            spec_id=row["spec_id"],
            version=row["version"],
            title=row["title"],
            kind=row["kind"],
            status=row["status"],
            scope=row.get("scope"),
            body=row.get("body") or "",
            source_path=row.get("source_path") or "",
        )

    invalidated_by: dict[str, str] = {}
    for row in specs:
        supersedes = row.get("supersedes")
        if not supersedes:
            continue
        for reference in str(supersedes).split(","):
            reference = reference.strip()
            if not reference:
                continue
            invalidated_by[_normalise_key(reference)] = f"{row['spec_id']}@{row['version']}"
    for row in specs:
        superseded_by = row.get("superseded_by")
        if superseded_by:
            invalidated_by[f"{row['spec_id']}@{row['version']}"] = superseded_by

    applicable: list[SpecView] = []
    invalidated: list[SpecView] = []
    for key, view in by_key.items():
        if view.status == "superseded" or key in invalidated_by:
            view.status = "superseded"
            view.superseded_by = invalidated_by.get(key)
            invalidated.append(view)
        else:
            applicable.append(view)
    applicable.sort(key=lambda item: (item.kind, item.key))
    invalidated.sort(key=lambda item: item.key)
    return applicable, invalidated
