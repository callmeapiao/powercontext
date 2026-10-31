# 验证样例运行记录

运行时间：2026-10-30T14:14:24+00:00

命令与退出码：

| 步骤 | 命令 | 退出码 | 预期 | 结果 |
| --- | --- | ---: | --- | --- |
| restore demo/orderflow/filters.py from snapshots/before | `shutil.copyfile` | 0 | 成功 | 通过 |
| reset runtime (seekdb + PowerContext state) | `ctxmod reset` | 0 | 成功 | 通过 |
| import materials (baseline copy) | `ctxmod --project <copy> import` | 0 | 成功 | 通过 |
| re-import with the updated specification | `ctxmod --project <copy> import` | 0 | 成功 | 通过 |
| updated specification is used by the module | `compare context_spec_v2.json and context_spec_v3.json` | 0 | 成功 | 通过 |
| reset runtime | `ctxmod reset` | 0 | 成功 | 通过 |
| import specifications, code and tests into seekdb | `ctxmod import` | 0 | 成功 | 通过 |
| target tests fail before the change | `python -m pytest demo/tests -q --tb=short` | 1 | 失败 | 通过 |
| build development context | `ctxmod context --json` | 0 | 成功 | 通过 |
| MiniMax modifies the code and the tests are re-run | `ctxmod run` | 0 | 成功 | 通过 |
| every test passes after the change | `python -m pytest demo/tests -q --tb=short` | 0 | 成功 | 通过 |
| read task state in a fresh process | `ctxmod state --task DEMO-1` | 0 | 成功 | 通过 |
| reuse the stored state to verify the task again | `ctxmod verify --task DEMO-1` | 0 | 成功 | 通过 |

## 规格演进证据

重新导入更新后的规格后，模块使用的适用规格发生变化（不是固定答案）：

```json
{"before": ["SPEC-001@1", "SPEC-002@2"], "after": ["SPEC-001@1", "SPEC-002@3"]}
```

## 相关文件

- `context.json`：本次任务构造出的开发上下文
- `import_report.json`：导入报告与 seekdb 查询记录
- `test_run_before.log`：修改前的测试结果（目标功能测试失败）
- `test_run_after.log`：修改后的测试结果（全部通过）
- `diff.patch`：MiniMax 修改产生的代码差异
- `minimax_response.md`：MiniMax 的原始回答
- `minimax_exchange.json`：MiniMax 调用记录（模型、耗时、token 用量）
- `state_after_restart.json`：重启后读回的任务状态
- `verify_after_restart.json`：重启后复用状态进行验证的结果
- `spec_evolution/`：规格更新前后的上下文对比
