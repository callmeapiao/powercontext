# 一页技术说明

## 1. 模块结构

```
                     ┌──────────────────────────────────────────────┐
   开发任务描述 ───▶ │ ctxmod.context.build_context                 │
                     │  关键词抽取 → 规格状态判定 → 资料定位         │
                     └───┬───────────────┬──────────────┬───────────┘
                         │               │              │
              ┌──────────▼───┐  ┌────────▼────────┐  ┌──▼──────────────┐
              │ ctxmod.db    │  │ ctxmod.state    │  │ ctxmod.agent    │
              │ seekdb 资料库 │  │ PowerContext    │  │ MiniMax 调用    │
              │ spec/code/   │  │ Source/Artifact │  │ Anthropic 协议  │
              │ test 四张表   │  │ 修订 + lineage  │  │                │
              └──────────────┘  └─────────────────┘  └─────────────────┘
                         │               │              │
                         └───────────────┴──────────────┘
                                         ▼
                          ctxmod.patch：写回文件 + 生成差异
```

模块分四层，职责单一、互相可替换：

| 文件 | 职责 | 依赖 |
| --- | --- | --- |
| `db.py` | 规格/代码/测试的导入、检索、统计；所有 SQL 在此执行 | `pylibseekdb` |
| `state.py` | 任务状态的保存与读取；实现 PowerContext 的 `ArtifactCatalog`/`ArtifactStore`/`SourceCatalogBackend`/`SourceStore` 协议 | `powercontext` |
| `context.py` | 规格状态判定、关键词抽取、上下文渲染 | 无（纯逻辑） |
| `agent.py` | MiniMax 调用与原始交换记录 | 标准库 `urllib` |
| `cli.py` | 命令行入口、流程编排、产物落盘 | 以上四者 |

## 2. 上下文选择方式

上下文**完全由数据决定**，没有写死的答案。

1. **关键词抽取**：英文按词、中文按二元组切分任务描述。
2. **规格状态判定**（`resolve_spec_status`）：
   * 条目自身 `status == superseded` → 失效；
   * 其它条目的 `supersedes` 指向它 → 失效，并记录被谁取代；
   * 其余 → **适用**。
   失效条目不会从上下文中消失，而是单独放在"已失效的决策（不要遵循）"一节，
   让模型知道冲突在哪、该以哪份为准。
3. **资料定位**：目标文件优先；符号与测试按关键词在 seekdb 中打分检索；
   `kind == target` 的测试用例无条件纳入。
4. **状态合并**：PowerContext 中该任务的最新修订（已完成 / 待办 / 已有验证结果）
   拼接到上下文末尾，供跨会话续作。

因此：重新导入一份更新后的规格后，适用规格集合会立刻改变
（样例中 `SPEC-001@1 + SPEC-002@2` → `SPEC-001@1 + SPEC-002@3`），
模块不会返回固定答案。

## 3. 各项技术的分工

| 技术 | 承担的作用 | 具体落点 |
| --- | --- | --- |
| **seekdb**（OceanBase AI 原生检索引擎） | 存储工程资料并提供检索 | `db.py` 的 `spec` / `code_file` / `code_symbol` / `test_case` 四张表；上下文中的规格、代码、测试全部来自这里的查询结果；导入报告里记录了实际执行的 SQL |
| **PowerContext** | 保存并复用任务状态 | `state.py` 实现其公开协议：导入的资料注册为 **Source**；每次保存任务状态写入一个 **Artifact 新修订**（`family=task-state`），artifact 的 **lineage** 记录它引用了哪些 Source；重启后 `state`/`verify` 命令读回并继续使用 |
| **MiniMax** | 参与本次代码生成与修改 | `agent.py` 以 Anthropic Messages 协议调用 `MiniMax-M3`，把 seekdb+PowerContext 组装的上下文交给模型，模型返回目标文件的新内容，模块落盘并重跑测试；原始提示词、回答、耗时与 token 用量全部留档 |

三者不是并列摆设，而是串在同一条链路上：
**seekdb 提供"资料"→ PowerContext 提供"进度"→ MiniMax 基于两者产出"修改"→ 测试验证"修改"→ 结果再写回 PowerContext。**

## 4. 与题目验收项的对应

| 验收项 | 本材料的落点 |
| --- | --- |
| 能够运行 | `README.md` 第 3–6 节；`scripts/run_sample.py` 一条命令复现 |
| MiniMax 实际参与 | `artifacts/minimax_response.md`、`artifacts/prompt.md`、`artifacts/minimax_exchange.json`，且 `artifacts/diff.patch` 与回答内容一致 |
| 上下文正确 | `artifacts/context.json`；规格演进对照见 `artifacts/spec_evolution/` |
| 数据库实际参与 | `artifacts/import_report.json` 中的 seekdb 行数与 SQL 记录 |
| 状态能够复用 | `artifacts/state_after_restart.json`、`artifacts/verify_after_restart.json`（均为独立进程执行） |
| 代码修改有效 | `artifacts/test_run_before.log`（6 failed / 6 passed）对比 `artifacts/test_run_after.log`（12 passed），测试集未做任何改动 |
