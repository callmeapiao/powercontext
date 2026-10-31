"""Unit tests of the context module itself.

Run them with::

    python -m pytest tests -q
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ctxmod.config import Settings  # noqa: E402
from ctxmod.context import extract_keywords, resolve_spec_status  # noqa: E402
from ctxmod.db import MaterialStore, _interpolate, _literal  # noqa: E402
from ctxmod.patch import extract_code_block, unified_diff  # noqa: E402
from ctxmod.specs import parse_front_matter, parse_spec_file  # noqa: E402
from ctxmod.state import TaskState, TaskStateStore  # noqa: E402


@contextlib.contextmanager
def temp_dir():
    """Yield a temporary directory and clean it up best effort.

    The embedded seekdb runtime keeps its log file open for a moment after
    ``close()`` on Windows, so the cleanup must not fail the test.
    """

    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_extract_keywords_handles_ascii_and_cjk():
    keywords = extract_keywords("给 filter_orders 增加 channel 渠道筛选条件")
    assert "filter_orders" in keywords
    assert "channel" in keywords
    assert "渠道" in keywords
    assert "筛选" in keywords


def test_parse_front_matter():
    meta, body = parse_front_matter("---\nid: SPEC-1\nversion: \"2\"\n---\n\n正文内容\n")
    assert meta["id"] == "SPEC-1"
    assert meta["version"] == "2"
    assert body.strip() == "正文内容"


def test_parse_spec_file(tmp_path=None):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        specs = base / "specs"
        specs.mkdir()
        path = specs / "SPEC-001.md"
        path.write_text(
            "---\nid: SPEC-001\nversion: \"1\"\ntitle: 标题\nkind: spec\nscope: a.b\n---\n\n正文\n",
            encoding="utf-8",
        )
        spec = parse_spec_file(path, base)
        assert spec.spec_id == "SPEC-001"
        assert spec.version == "1"
        assert spec.scope == "a.b"
        assert spec.source_path == "specs/SPEC-001.md"
        assert spec.body == "正文"


def test_resolve_spec_status_marks_superseded_entries():
    specs = [
        {"spec_id": "SPEC-002", "version": "1", "title": "旧", "kind": "spec", "status": "active",
         "supersedes": None, "superseded_by": None, "scope": None, "body": "", "source_path": "a"},
        {"spec_id": "SPEC-002", "version": "2", "title": "新", "kind": "spec", "status": "active",
         "supersedes": "SPEC-002@v1", "superseded_by": None, "scope": None, "body": "", "source_path": "b"},
        {"spec_id": "DECISION-001", "version": "1", "title": "决策", "kind": "decision", "status": "active",
         "supersedes": None, "superseded_by": None, "scope": None, "body": "", "source_path": "c"},
    ]
    applicable, invalidated = resolve_spec_status(specs)
    assert [item.key for item in applicable] == ["DECISION-001@1", "SPEC-002@2"]
    assert [item.key for item in invalidated] == ["SPEC-002@1"]
    assert invalidated[0].superseded_by == "SPEC-002@2"


def test_literal_escaping_is_safe():
    assert _literal("O'Reilly") == "'O''Reilly'"
    assert _literal("a\\b") == "'a\\\\b'"
    assert _literal(None) == "NULL"
    assert _literal(3) == "3"
    assert _interpolate("SELECT ? FROM t WHERE x = ?", ("a'b", "c")) == "SELECT 'a''b' FROM t WHERE x = 'c'"


def test_extract_code_block_prefers_marker():
    answer = "说明\n```python\nprint(1)\n```\n最终版本\n```python\nprint(2)\n```\n"
    assert extract_code_block(answer) == "print(2)"
    assert extract_code_block(answer, prefer="print(1)") == "print(1)"


def test_unified_diff_reports_change():
    diff = unified_diff("a.py", "one\n", "two\n")
    assert "one" in diff and "two" in diff


def test_material_store_roundtrip():
    with temp_dir() as tmp:
        settings = Settings(
            root=Path(tmp),
            seekdb_dir=Path(tmp) / "seekdb",
            state_db=Path(tmp) / "state.sqlite3",
            log_dir=Path(tmp) / "logs",
        )
        store = MaterialStore(settings.seekdb_dir, settings.database).open()
        try:
            store.upsert_spec(
                spec_id="SPEC-1", version="1", title="渠道筛选", kind="spec", status="active",
                supersedes=None, scope="a.b", body="按渠道筛选", source_path="specs/SPEC-1.md",
                imported_at="2026-01-01T00:00:00+00:00",
            )
            store.upsert_code_file("m.py", "python", "def f():\n    pass\n")
            store.upsert_symbol("m.py", "f", "function", "f()", "def f():\n    pass\n", 1)
            store.upsert_test_case("t.py", "test_f", "target", "def test_f():\n    pass\n")

            rows = store.specs()
            assert rows[0]["title"] == "渠道筛选"
            assert store.stats()["spec"] == 1
            assert store.stats()["code_symbol"] == 1
            assert store.stats()["test_case"] == 1
            assert store.code_files()[0]["content"].startswith("def f()")
        finally:
            store.close()


def test_task_state_is_persisted_as_artifact_revisions():
    with temp_dir() as tmp:
        settings = Settings(
            root=Path(tmp),
            seekdb_dir=Path(tmp) / "seekdb",
            state_db=Path(tmp) / "state.sqlite3",
            log_dir=Path(tmp) / "logs",
        )
        store = TaskStateStore(settings)
        try:
            state = TaskState(task_id="T1", description="做一个改动")
            state.done.append("第一步")
            first = asyncio.run(store.save(state))
            assert first.revision == 1

            state.done.append("第二步")
            second = asyncio.run(store.save(state))
            assert second.revision == 2

            loaded = asyncio.run(store.load("T1"))
            assert loaded is not None
            assert loaded.revision == 2
            assert loaded.content.done == ["第一步", "第二步"]

            history = asyncio.run(store.history("T1"))
            assert [item.revision for item in history] == [1, 2]
        finally:
            store.close()


def test_task_state_survives_a_new_store_instance():
    with temp_dir() as tmp:
        settings = Settings(
            root=Path(tmp),
            seekdb_dir=Path(tmp) / "seekdb",
            state_db=Path(tmp) / "state.sqlite3",
            log_dir=Path(tmp) / "logs",
        )
        store = TaskStateStore(settings)
        asyncio.run(store.save(TaskState(task_id="T2", description="重启前")))
        store.close()

        reopened = TaskStateStore(settings)
        try:
            loaded = asyncio.run(reopened.load("T2"))
            assert loaded is not None
            assert loaded.content.description == "重启前"
        finally:
            reopened.close()
