"""Command line entry point of the SDD context module.

    python -m ctxmod init
    python -m ctxmod import --project demo
    python -m ctxmod context --task T1 --description "..."
    python -m ctxmod run --task T1 --description "..." --target orderflow/filters.py
    python -m ctxmod state --task T1
    python -m ctxmod verify --task T1
    python -m ctxmod reset
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent import MiniMaxClient, MiniMaxError
from .config import Settings
from .context import DevelopmentContext, extract_keywords, resolve_spec_status
from .db import MaterialStore
from .patch import extract_code_block, unified_diff, write_file
from .specs import (
    extract_python_symbols,
    extract_test_cases,
    parse_spec_file,
)
from .state import TaskState, TaskStateStore, ValidationRecord

DEFAULT_PROJECT = Path(__file__).resolve().parent.parent / "demo"


# --------------------------------------------------------------------- helpers
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.load(Path(__file__).resolve().parent.parent)


def _project(args: argparse.Namespace) -> Path:
    project = Path(args.project).resolve() if args.project else DEFAULT_PROJECT
    if not project.exists():
        raise SystemExit(f"project directory not found: {project}")
    return project


def _artifacts_dir(settings: Settings, args: argparse.Namespace) -> Path:
    base = Path(args.out).resolve() if args.out else settings.root / "artifacts"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------- import
def collect_materials(project: Path) -> dict[str, Any]:
    """Collect specifications, code and tests from a project directory."""

    specs = []
    spec_dir = project / "specs"
    if spec_dir.is_dir():
        for path in sorted(spec_dir.glob("*.md")):
            specs.append(parse_spec_file(path, project))

    files: list[tuple[str, str, str]] = []
    symbols = []
    tests = []
    for path in sorted(project.rglob("*.py")):
        relative = path.relative_to(project).as_posix()
        if relative.startswith("tests/"):
            tests.extend(extract_test_cases(path, project))
        else:
            content = path.read_text(encoding="utf-8")
            files.append((relative, "python", content))
            symbols.extend(extract_python_symbols(path, project))
    return {"specs": specs, "files": files, "symbols": symbols, "tests": tests}


def cmd_import(args: argparse.Namespace) -> int:
    """Import engineering material into seekdb and register it in PowerContext."""

    settings = _settings(args)
    settings.ensure_dirs()
    project = _project(args)
    materials = collect_materials(project)
    store = MaterialStore(settings.seekdb_dir, settings.database).open()
    state = TaskStateStore(settings)
    try:
        return asyncio.run(_import_materials(args, settings, project, materials, store, state))
    finally:
        state.close()
        store.close()


async def _import_materials(
    args: argparse.Namespace,
    settings: Settings,
    project: Path,
    materials: dict[str, Any],
    store: MaterialStore,
    state: TaskStateStore,
) -> int:
    """Import the collected material into both stores."""
    # A fresh import starts from a clean material table so that a removed
    # specification cannot survive in the store.
    store.execute("DELETE FROM spec")
    store.execute("DELETE FROM code_file")
    store.execute("DELETE FROM code_symbol")
    store.execute("DELETE FROM test_case")

    for spec in materials["specs"]:
        store.upsert_spec(
            spec_id=spec.spec_id,
            version=spec.version,
            title=spec.title,
            kind=spec.kind,
            status=spec.status,
            supersedes=spec.supersedes,
            scope=spec.scope,
            body=spec.body,
            source_path=spec.source_path,
            imported_at=_now(),
        )

    # Apply the supersedes links: the replaced entry becomes invalid.
    for spec in materials["specs"]:
        if spec.supersedes:
            for reference in spec.supersedes.split(","):
                reference = reference.strip()
                if not reference:
                    continue
                target = reference if "@" in reference else f"{reference}@1"
                spec_id, _, version = target.partition("@")
                store.mark_superseded(spec_id, version.lstrip("v") or "1", spec.key)

    for path, language, content in materials["files"]:
        store.upsert_code_file(path, language, content)
    for symbol in materials["symbols"]:
        store.upsert_symbol(
            symbol.path, symbol.symbol, symbol.kind, symbol.signature, symbol.body, symbol.line_start
        )
    for test in materials["tests"]:
        store.upsert_test_case(test.path, test.test_name, test.kind, test.body)

    registered = []
    for spec in materials["specs"]:
        await_state = await state.register_material(
            name=f"spec:{spec.source_path}",
            kind="spec",
            path=spec.source_path,
            payload=spec.body,
            digest=spec.key,
        )
        registered.append(await_state.name)
    for path, _language, content in materials["files"]:
        source = await state.register_material(
            name=f"code:{path}", kind="code", path=path, payload=content, digest=path
        )
        registered.append(source.name)
    for test in materials["tests"]:
        source = await state.register_material(
            name=f"test:{test.path}::{test.test_name}",
            kind="test",
            path=test.path,
            payload=test.body,
            digest=test.test_name,
        )
        registered.append(source.name)

    stats = store.stats()
    report = {
        "imported_at": _now(),
        "project": settings.relative(project),
        "specs": len(materials["specs"]),
        "code_files": len(materials["files"]),
        "code_symbols": len(materials["symbols"]),
        "test_cases": len(materials["tests"]),
        "seekdb": stats,
        "powercontext_materials": len(registered),
        "queries": store.query_log,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------- context
async def build_context(
    args: argparse.Namespace,
    settings: Settings,
    project: Path,
    store: MaterialStore,
    state_store: TaskStateStore,
) -> DevelopmentContext:
    """Assemble the development context for a task."""

    description = args.description or ""
    keywords = extract_keywords(description)
    all_specs = store.specs()
    applicable, invalidated = resolve_spec_status(all_specs)

    target_file = (args.target or "").replace("\\", "/")
    code_files = []
    for row in store.code_files():
        if target_file and row["path"] == target_file:
            code_files.append(row)
        elif keywords and any(keyword in row["path"].lower() for keyword in keywords):
            code_files.append(row)
    if not code_files and target_file:
        code_files = [row for row in store.code_files() if target_file in row["path"]]
    if not code_files:
        code_files = store.code_files()[:4]

    symbols: dict[tuple[str, str], dict[str, Any]] = {}
    for row in store.search_symbols(keywords, limit=12):
        row.pop("score", None)
        symbols[(row["path"], row["symbol"])] = row
    if target_file:
        for row in store.symbols():
            if row["path"] == target_file:
                symbols.setdefault((row["path"], row["symbol"]), row)

    tests: dict[tuple[str, str], dict[str, Any]] = {}
    for row in store.search_tests(keywords, limit=12):
        row.pop("score", None)
        tests[(row["path"], row["test_name"])] = row
    for row in store.test_cases():
        if row["kind"] == "target":
            tests.setdefault((row["path"], row["test_name"]), row)

    artifact = await state_store.load(args.task)
    task_state = artifact.content if artifact else None

    return DevelopmentContext(
        task_id=args.task,
        description=description,
        keywords=keywords,
        applicable_specs=applicable,
        invalidated_specs=invalidated,
        code_files=code_files,
        symbols=list(symbols.values()),
        tests=list(tests.values()),
        state=task_state,
    )


def cmd_context(args: argparse.Namespace) -> int:
    """Print the development context of a task."""

    settings = _settings(args)
    project = _project(args)
    store = MaterialStore(settings.seekdb_dir, settings.database).open()
    state_store = TaskStateStore(settings)
    try:
        context = asyncio.run(build_context(args, settings, project, store, state_store))
        if args.json:
            print(json.dumps(context.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(context.render())
        return 0
    finally:
        state_store.close()
        store.close()


# ------------------------------------------------------------------------- run
def build_prompt(context: DevelopmentContext, target_file: str) -> str:
    """Build the prompt handed to MiniMax."""

    return f"""你是一名资深开发助手。请依据下面提供的工程上下文完成一次代码修改。

{context.render()}

要求：
1. 只能依据"当前适用的规格条目"实现功能；"已失效的决策"仅用于识别冲突，不得照其实现。
2. 只允许修改文件 `{target_file}`，不要改动其他文件。
3. 保持文件中既有行为不变（回归测试必须继续通过）。
4. 输出该文件的**完整最新内容**，放在一个 ```python 代码块中，除代码块外不要输出多余解释。

现在输出 `{target_file}` 的完整最新内容：
"""


def run_tests(project: Path, extra_args: list[str] | None = None) -> dict[str, Any]:
    """Run the project's test suite and capture the outcome."""

    tests_dir = project / "tests"
    command = [sys.executable, "-m", "pytest", str(tests_dir), "-q", "--tb=short"]
    if extra_args:
        command.extend(extra_args)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            cwd=str(project.parent),
        )
    except FileNotFoundError:
        command = [sys.executable, "-m", "unittest", "discover", "-s", str(tests_dir), "-t", str(project)]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            cwd=str(project.parent),
        )
    output = (completed.stdout or "") + (completed.stderr or "")
    passed = failed = 0
    match = re.search(r"(\d+) passed", output)
    if match:
        passed = int(match.group(1))
    match = re.search(r"(\d+) failed", output)
    if match:
        failed = int(match.group(1))
    return {
        "command": " ".join(command),
        "exit_code": completed.returncode,
        "passed": passed,
        "failed": failed,
        "output": output,
    }


def cmd_run(args: argparse.Namespace) -> int:
    """Run the whole flow: context, MiniMax modification, tests, state."""

    settings = _settings(args)
    settings.ensure_dirs()
    project = _project(args)
    artifacts = _artifacts_dir(settings, args)
    store = MaterialStore(settings.seekdb_dir, settings.database).open()
    state_store = TaskStateStore(settings)
    try:
        context = asyncio.run(build_context(args, settings, project, store, state_store))
        target = (args.target or "").replace("\\", "/")
        if not target:
            raise SystemExit("--target is required: name the file the assistant may rewrite")

        prompt = build_prompt(context, target)
        client = MiniMaxClient(model=args.model or settings.minimax_model)
        try:
            exchange = client.complete(prompt)
        except MiniMaxError as exc:
            print(f"MiniMax call failed: {exc}", file=sys.stderr)
            return 2

        new_content = extract_code_block(exchange.response_text)
        target_path = project / target
        before = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
        diff = unified_diff(target, before, new_content)

        _write(artifacts / "context.md", context.render())
        _write(artifacts / "prompt.md", prompt)
        _write(artifacts / "minimax_response.md", exchange.response_text)
        _write(artifacts / "minimax_exchange.json", json.dumps(exchange.summary(), ensure_ascii=False, indent=2))
        _write(artifacts / "diff.patch", diff)
        backup = artifacts / "original" / target
        if before:
            _write(backup, before)

        if not args.no_apply:
            write_file(target_path, new_content)

        result = run_tests(project)
        _write(artifacts / "test_run.log", result["output"])

        state = context.state or TaskState(task_id=args.task, description=context.description)
        state.description = context.description or state.description
        state.keywords = context.keywords
        state.active_specs = [spec.key for spec in context.applicable_specs]
        state.superseded_specs = [spec.key for spec in context.invalidated_specs]
        state.target_symbols = [f"{item['path']}::{item['symbol']}" for item in context.symbols]
        state.code_files = [item["path"] for item in context.code_files]
        state.test_paths = sorted({item["path"] for item in context.tests})
        state.material_sources = [f"spec:{spec.source_path}" for spec in context.applicable_specs]
        done = list(state.done)
        if result["exit_code"] == 0:
            done.append(f"修改 {target} 并通过全部测试")
        state.done = done
        todo = [item for item in state.todo if item != f"修改 {target}"]
        if result["exit_code"] != 0:
            todo.append(f"修改 {target}（当前测试未通过：exit={result['exit_code']}）")
        state.todo = todo
        state.validations.append(
            ValidationRecord(
                command=result["command"],
                exit_code=result["exit_code"],
                passed=result["passed"],
                failed=result["failed"],
                summary=f"{result['passed']} passed, {result['failed']} failed",
                recorded_at=_now(),
            )
        )
        artifact = asyncio.run(state_store.save(state))

        summary = {
            "task_id": args.task,
            "target": target,
            "minimax": exchange.summary(),
            "diff_bytes": len(diff),
            "applied": not args.no_apply,
            "tests": {k: v for k, v in result.items() if k != "output"},
            "state_revision": artifact.revision,
            "artifacts": settings.relative(artifacts),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if result["exit_code"] == 0 else 1
    finally:
        state_store.close()
        store.close()


# ----------------------------------------------------------------------- state
def cmd_state(args: argparse.Namespace) -> int:
    """Show the task state persisted by PowerContext."""

    settings = _settings(args)
    state_store = TaskStateStore(settings)
    try:
        artifact = asyncio.run(state_store.load(args.task))
        if artifact is None:
            print(f"no state stored for task {args.task}")
            return 1
        history = asyncio.run(state_store.history(args.task))
        print(
            json.dumps(
                {
                    "task_id": args.task,
                    "revision": artifact.revision,
                    "revisions": len(history),
                    "state": artifact.content.model_dump(),
                    "lineage_sources": [ref.source_id for ref in artifact.lineage.sources],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        state_store.close()


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-run the tests using the state stored by an earlier run."""

    settings = _settings(args)
    project = _project(args)
    state_store = TaskStateStore(settings)
    try:
        artifact = asyncio.run(state_store.load(args.task))
        if artifact is None:
            print(f"no state stored for task {args.task}", file=sys.stderr)
            return 1
        state = artifact.content
        result = run_tests(project)
        print(
            json.dumps(
                {
                    "task_id": args.task,
                    "restored_revision": artifact.revision,
                    "done": state.done,
                    "todo": state.todo,
                    "previous_validations": [v.model_dump() for v in state.validations],
                    "tests": {k: v for k, v in result.items() if k != "output"},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result["exit_code"] == 0 else 1
    finally:
        state_store.close()


# ------------------------------------------------------------------ init/reset
def cmd_init(args: argparse.Namespace) -> int:
    """Create the runtime directories and initialise both stores."""

    settings = _settings(args)
    settings.ensure_dirs()
    store = MaterialStore(settings.seekdb_dir, settings.database).open()
    state_store = TaskStateStore(settings)
    try:
        print(
            json.dumps(
                {
                    "seekdb_dir": str(settings.seekdb_dir),
                    "state_db": str(settings.state_db),
                    "database": settings.database,
                    "seekdb": store.stats(),
                    "powercontext": "ready",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        state_store.close()
        store.close()


def cmd_reset(args: argparse.Namespace) -> int:
    """Remove the runtime directory so a sample can start from empty state."""

    settings = _settings(args)
    runtime = settings.seekdb_dir.parent
    if runtime.exists():
        shutil.rmtree(runtime)
    print(json.dumps({"removed": str(runtime)}, ensure_ascii=False))
    return 0


# ------------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctxmod", description="SDD context module for AI development assistants")
    parser.add_argument("--project", help="business project directory (default: the bundled demo)")
    parser.add_argument("--out", help="directory for run artifacts (default: minimax/artifacts)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="initialise seekdb and the PowerContext state database").set_defaults(func=cmd_init)
    sub.add_parser("import", help="import specifications, code and tests into seekdb").set_defaults(func=cmd_import)

    context_parser = sub.add_parser("context", help="print the development context of a task")
    context_parser.add_argument("--task", required=True)
    context_parser.add_argument("--description", required=True)
    context_parser.add_argument("--target", help="file the assistant is expected to change")
    context_parser.add_argument("--json", action="store_true", help="print structured JSON instead of text")
    context_parser.set_defaults(func=cmd_context)

    run_parser = sub.add_parser("run", help="build context, call MiniMax, apply the change and test it")
    run_parser.add_argument("--task", required=True)
    run_parser.add_argument("--description", required=True)
    run_parser.add_argument("--target", required=True)
    run_parser.add_argument("--model", help="MiniMax model id")
    run_parser.add_argument("--no-apply", action="store_true", help="do not write the modified file")
    run_parser.set_defaults(func=cmd_run)

    state_parser = sub.add_parser("state", help="show the task state stored by PowerContext")
    state_parser.add_argument("--task", required=True)
    state_parser.set_defaults(func=cmd_state)

    verify_parser = sub.add_parser("verify", help="re-run the tests using the stored task state")
    verify_parser.add_argument("--task", required=True)
    verify_parser.set_defaults(func=cmd_verify)

    sub.add_parser("reset", help="delete the runtime directory").set_defaults(func=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
