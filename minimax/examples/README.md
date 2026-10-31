# 验证样例

完整定义见 `task.json`，下面是人读版。

## 1. 任务描述

> 给 `filter_orders` 增加 `channel` 渠道筛选条件，需要支持 `mini_program` 渠道。

业务工程：`demo/orderflow`（订单筛选服务）。

* 要改的代码：`demo/orderflow/filters.py` 的 `filter_orders`
* 验证用测试：`demo/tests/test_filters_target.py`（目标功能）、`demo/tests/test_filters_regression.py`（回归）

## 2. 本次任务适用的规格（必须遵守）

| 键 | 标题 | 来源 |
| --- | --- | --- |
| `SPEC-001@1` | 订单筛选基本语义 | `demo/specs/SPEC-001-order-status.md` |
| `SPEC-002@2` | 渠道筛选条件 | `demo/specs/SPEC-002-channel-filter-v2.md` |

`SPEC-002@2` 的要点：参数名 `channel`；渠道取值 `app` / `web` / `mini_program`；
比较**大小写不敏感**；`channel=None` 不过滤；订单缺少 `channel` 字段时不被选中。

## 3. 已失效、不得遵循的条目

| 键 | 被谁取代 | 失效原因 |
| --- | --- | --- |
| `SPEC-002@1` | `SPEC-002@2` | 渠道取值与匹配语义被取代 |
| `DECISION-001@1` | `SPEC-002@2` | “精确匹配且区分大小写”的决策被取代 |

> 若只看到 v1 与 DECISION-001，实现会做成大小写敏感、且不支持 `mini_program`，
> 目标测试 `test_filter_orders_channel_is_case_insensitive` 与
> `test_filter_orders_channel_accepts_new_channel_value` 就会失败。
> 这正是本模块要解决的问题：**告诉助手该听哪一份**。

## 4. 初始数据

| 类别 | 文件 |
| --- | --- |
| 规格 | `demo/specs/SPEC-001-order-status.md`、`SPEC-002-channel-filter-v1.md`、`SPEC-002-channel-filter-v2.md`、`DECISION-001-channel-matching.md` |
| 代码 | `demo/orderflow/filters.py`、`demo/orderflow/__init__.py` |
| 测试 | `demo/tests/test_filters_target.py`、`demo/tests/test_filters_regression.py` |
| 修改前快照 | `snapshots/before/orderflow/filters.py` |
| 修改后快照 | `snapshots/after/orderflow/filters.py` |

## 5. 操作步骤

一条命令：

```bash
cd minimax
python scripts/run_sample.py
```

脚本依次做 9 件事，每一步的命令与退出码都写进 `artifacts/REPORT.md`：

1. 用 `snapshots/before/` 恢复修改前的业务代码；
2. `ctxmod reset` 清空运行时（seekdb + PowerContext 状态库），从空状态开始；
3. 在副本上做**规格演进对照**：导入 → 记录上下文 → 发布 `SPEC-002@v3` → 重新导入 → 再记录上下文，
   证明适用规格随导入内容变化（`SPEC-002@2` → `SPEC-002@3`），模块不是固定答案；
4. 回到真实工程，重新 `ctxmod import`；
5. **修改前**跑固定测试集（预期目标功能测试失败）；
6. `ctxmod context` 组装开发上下文；
7. `ctxmod run`：把上下文交给 MiniMax → 模型返回新文件 → 落盘 → 重跑测试 → 写状态；
8. **修改后**再跑同一套测试（预期全部通过）；
9. 在**新进程**中 `ctxmod state` 读回状态、`ctxmod verify` 复用状态再次验证。

也可以逐步执行：

```bash
python -m ctxmod reset
python -m ctxmod import
python -m pytest demo/tests -q --tb=short     # 6 failed, 6 passed
python -m ctxmod context --task DEMO-1 \
    --description "给 filter_orders 增加 channel 渠道筛选条件，需要支持 mini_program 渠道" \
    --target orderflow/filters.py
python -m ctxmod run --task DEMO-1 \
    --description "给 filter_orders 增加 channel 渠道筛选条件，需要支持 mini_program 渠道" \
    --target orderflow/filters.py
python -m pytest demo/tests -q --tb=short     # 12 passed
python -m ctxmod state --task DEMO-1
python -m ctxmod verify --task DEMO-1
```

## 6. 预期结果

| 阶段 | 预期 |
| --- | --- |
| 修改前 | 目标功能测试 6 个失败（`filter_orders()` 不接受 `channel` 关键字），回归测试 6 个通过 |
| 修改后 | 12 个测试全部通过 |
| 状态复用 | 新进程能读回状态（`done`/`todo`/`validations`/`active_specs`/`superseded_specs`），并用于再次验证 |
| 规格演进 | 重新导入 `SPEC-002@v3` 后，适用规格由 `SPEC-001@1 + SPEC-002@2` 变为 `SPEC-001@1 + SPEC-002@3` |
| 数据库 | seekdb 中 `spec=4`、`code_file=2`、`code_symbol=1`、`test_case=12`，上下文全部来自这些表的查询结果 |

对应证据文件：

* `artifacts/test_run_before.log` / `artifacts/test_run_after.log`
* `artifacts/context.json` / `artifacts/prompt.md`
* `artifacts/minimax_response.md` / `artifacts/minimax_exchange.json` / `artifacts/diff.patch`
* `artifacts/state_after_restart.json` / `artifacts/verify_after_restart.json`
* `artifacts/spec_evolution/context_spec_v2.json` / `context_spec_v3.json`
* `artifacts/import_report.json` / `artifacts/REPORT.md`

## 7. 重置方法

```bash
# 清空运行时：删除 seekdb 数据目录与 PowerContext 状态库
python -m ctxmod reset

# 把业务代码恢复到修改前
cp snapshots/before/orderflow/filters.py demo/orderflow/filters.py
```

两条命令做完即回到完全空的状态，可以重新从第 1 步开始。
`python scripts/run_sample.py` 会自动完成上述两步并重跑整个流程。
