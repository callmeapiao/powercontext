#!/usr/bin/env python3
"""Run the complete verification sample of the SDD context module.

    python scripts/run_sample.py

The script performs, in order:

1. restore the business project to its pre-change snapshot,
2. reset the runtime so the sample starts from empty state,
3. import the specifications, code and tests into seekdb,
4. run the fixed test set **before** the change (the target tests must fail),
5. build the development context and show that a re-imported specification
   replaces the previous one,
6. let MiniMax perform the code change and apply it,
7. run the same test set **after** the change (every test must pass),
8. restart the module in a new process and read the stored task state back.

Every step, command and exit code is written to ``artifacts/REPORT.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
TASK_ID = "DEMO-1"
DESCRIPTION = "给 filter_orders 增加 channel 渠道筛选条件，需要支持 mini_program 渠道"
TARGET = "orderflow/filters.py"

steps: list[dict] = []


def record(name: str, command: str, exit_code: int, output: str = "", expect: str = "zero") -> None:
    """Record one step.

    ``expect`` is ``"zero"`` for a step that must succeed and ``"nonzero"``
    for a step that must fail (the pre-change test run, whose failure is the
    evidence that the target behaviour was not implemented yet).
    """

    succeeded = (exit_code != 0) if expect == "nonzero" else (exit_code == 0)
    steps.append(
        {
            "name": name,
            "command": command,
            "exit_code": exit_code,
            "output": output,
            "expect": expect,
            "succeeded": succeeded,
        }
    )
    mark = "ok " if succeeded else "FAIL"
    print(f"[{mark}] {name} (exit={exit_code}, expected {expect})")


def run(command: list[str], cwd: Path = ROOT) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def ctxmod(*args: str) -> tuple[int, str]:
    return run([sys.executable, "-m", "ctxmod", *args])


def pytest_run(project: Path) -> tuple[int, str]:
    return run([sys.executable, "-m", "pytest", str(project / "tests"), "-q", "--tb=short"], cwd=project.parent)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def restore_before_snapshot() -> None:
    source = ROOT / "snapshots" / "before" / "orderflow" / "filters.py"
    destination = ROOT / "demo" / "orderflow" / "filters.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def spec_evolution_demo() -> tuple[int, str]:
    """Show that a re-imported specification replaces the previous one."""

    workspace = ROOT / ".runtime" / "spec_evolution"
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(ROOT / "demo", workspace / "demo")
    specs = workspace / "demo" / "specs"

    code, output = ctxmod("--project", str(workspace / "demo"), "import")
    record("import materials (baseline copy)", f"ctxmod --project <copy> import", code, output)

    code, output = ctxmod(
        "--project", str(workspace / "demo"), "context", "--task", "EVOLVE", "--description", DESCRIPTION,
        "--target", TARGET, "--json",
    )
    before = json.loads(output)
    write(ARTIFACTS / "spec_evolution" / "context_spec_v2.json", json.dumps(before, ensure_ascii=False, indent=2))

    # A newer specification is published and replaces the previous version.
    write(
        specs / "SPEC-002-channel-filter-v3.md",
        """---
id: SPEC-002
version: "3"
title: 渠道筛选条件
kind: spec
status: active
supersedes: SPEC-002@v2
scope: orderflow.filter_orders
---

`filter_orders` 需要支持按渠道筛选，参数名为 `channel`。

- 渠道取值扩展为 `app`、`web`、`mini_program` 三种，`mini_program` 为新增渠道。
- 筛选按**大小写不敏感**进行：比较前双方都转换为小写。
- 传入 `channel=None` 表示不按渠道过滤。
- 订单缺少 `channel` 字段时，该订单在任何渠道筛选条件下都不被选中。
- 补充说明：参数名使用 snake_case；未知渠道值（例如 `unknown`）应返回空结果而不是报错。

本版本作废 `SPEC-002@v2`，实现必须以本版本为准。
""",
    )

    code, output = ctxmod("--project", str(workspace / "demo"), "import")
    record("re-import with the updated specification", f"ctxmod --project <copy> import", code, output)

    code, output = ctxmod(
        "--project", str(workspace / "demo"), "context", "--task", "EVOLVE", "--description", DESCRIPTION,
        "--target", TARGET, "--json",
    )
    after = json.loads(output)
    write(ARTIFACTS / "spec_evolution" / "context_spec_v3.json", json.dumps(after, ensure_ascii=False, indent=2))

    before_keys = [s["key"] for s in before["applicable_specs"]]
    after_keys = [s["key"] for s in after["applicable_specs"]]
    print(f"       applicable before: {before_keys}")
    print(f"       applicable after : {after_keys}")
    ok = "SPEC-002@3" in after_keys and "SPEC-002@3" not in before_keys
    record(
        "updated specification is used by the module",
        "compare context_spec_v2.json and context_spec_v3.json",
        0 if ok else 1,
        json.dumps({"before": before_keys, "after": after_keys}, ensure_ascii=False),
    )
    return (0 if ok else 1), json.dumps({"before": before_keys, "after": after_keys}, ensure_ascii=False)


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    print("== 1. restore the pre-change snapshot ==")
    restore_before_snapshot()
    record("restore demo/orderflow/filters.py from snapshots/before", "shutil.copyfile", 0)

    print("== 2. reset the runtime ==")
    code, output = ctxmod("reset")
    record("reset runtime (seekdb + PowerContext state)", "ctxmod reset", code, output)

    print("== 3. show that a re-imported specification replaces the previous one ==")
    evolution_code, evolution_output = spec_evolution_demo()

    print("== 4. reset again and import the real project ==")
    code, output = ctxmod("reset")
    record("reset runtime", "ctxmod reset", code, output)
    code, output = ctxmod("import")
    record("import specifications, code and tests into seekdb", "ctxmod import", code, output)
    write(ARTIFACTS / "import_report.json", output)

    print("== 5. tests BEFORE the change ==")
    code, output = pytest_run(ROOT / "demo")
    write(ARTIFACTS / "test_run_before.log", output)
    record(
        "target tests fail before the change",
        "python -m pytest demo/tests -q --tb=short",
        code,
        output,
        expect="nonzero",
    )

    print("== 6. build the development context ==")
    code, output = ctxmod(
        "context", "--task", TASK_ID, "--description", DESCRIPTION, "--target", TARGET, "--json"
    )
    record("build development context", "ctxmod context --json", code, output)
    write(ARTIFACTS / "context.json", output)

    print("== 7. MiniMax performs the code change ==")
    code, output = ctxmod(
        "run", "--task", TASK_ID, "--description", DESCRIPTION, "--target", TARGET
    )
    record("MiniMax modifies the code and the tests are re-run", "ctxmod run", code, output)
    write(ARTIFACTS / "run_report.json", output)

    print("== 8. tests AFTER the change ==")
    code, output = pytest_run(ROOT / "demo")
    write(ARTIFACTS / "test_run_after.log", output)
    record("every test passes after the change", "python -m pytest demo/tests -q --tb=short", code, output)

    print("== 9. restart the module and read the stored state back ==")
    code, output = ctxmod("state", "--task", TASK_ID)
    record("read task state in a fresh process", "ctxmod state --task DEMO-1", code, output)
    write(ARTIFACTS / "state_after_restart.json", output)

    code, output = ctxmod("verify", "--task", TASK_ID)
    record("reuse the stored state to verify the task again", "ctxmod verify --task DEMO-1", code, output)
    write(ARTIFACTS / "verify_after_restart.json", output)

    failed = [s for s in steps if not s["succeeded"]]
    write(ARTIFACTS / "REPORT.md", render_report(evolution_output, failed))
    print()
    print(f"report written to {ARTIFACTS / 'REPORT.md'}")
    if failed:
        print(f"{len(failed)} step(s) failed: {[s['name'] for s in failed]}")
        return 1
    print("all steps completed")
    return 0


def render_report(evolution_output: str, failed: list[dict]) -> str:
    lines = [
        "# 验证样例运行记录",
        "",
        f"运行时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "命令与退出码：",
        "",
        "| 步骤 | 命令 | 退出码 | 预期 | 结果 |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for step in steps:
        command = step["command"].replace("|", "\\|")
        expect = "成功" if step["expect"] == "zero" else "失败"
        verdict = "通过" if step["succeeded"] else "未通过"
        lines.append(f"| {step['name']} | `{command}` | {step['exit_code']} | {expect} | {verdict} |")
    lines += [
        "",
        "## 规格演进证据",
        "",
        "重新导入更新后的规格后，模块使用的适用规格发生变化（不是固定答案）：",
        "",
        "```json",
        evolution_output,
        "```",
        "",
        "## 相关文件",
        "",
        "- `context.json`：本次任务构造出的开发上下文",
        "- `import_report.json`：导入报告与 seekdb 查询记录",
        "- `test_run_before.log`：修改前的测试结果（目标功能测试失败）",
        "- `test_run_after.log`：修改后的测试结果（全部通过）",
        "- `diff.patch`：MiniMax 修改产生的代码差异",
        "- `minimax_response.md`：MiniMax 的原始回答",
        "- `minimax_exchange.json`：MiniMax 调用记录（模型、耗时、token 用量）",
        "- `state_after_restart.json`：重启后读回的任务状态",
        "- `verify_after_restart.json`：重启后复用状态进行验证的结果",
        "- `spec_evolution/`：规格更新前后的上下文对比",
        "",
    ]
    if failed:
        lines += ["## 未通过项", ""]
        for step in failed:
            lines.append(f"- **{step['name']}**：退出码 {step['exit_code']}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
