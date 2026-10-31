# 技术使用记录

以下为本次任务中三项指定技术的实际调用记录，均可在 `artifacts/` 下逐条核对。

---

## 一、MiniMax 开发记录

### 1.1 调用方式

* 协议：Anthropic Messages（`POST {baseURL}/messages`），与 MiniMax Code 配置中
  `provider.minimax.npm = @ai-sdk/anthropic` 对应；
* 模型：`MiniMax-M3`；
* 凭据：随 MiniMax Code 登录态获取的 access token（**不落盘、不入库**，仓库内不含任何真实凭据）。

### 1.2 一次完整调用的输入输出

| 项目 | 值 |
| --- | --- |
| 提示词长度 | 7223 字符（`artifacts/prompt.md`） |
| 回答长度 | 1716 字符（`artifacts/minimax_response.md`） |
| 耗时 | 2986 ms |
| 输入 token | 2128（cache read） |
| 输出 token | 375 |

提示词由模块自动组装，包含四段：任务描述、**当前适用的规格条目**、**已失效的决策**、
需修改的代码与验证用测试；并明确要求"只能依据当前适用的规格条目实现功能，
已失效的决策仅用于识别冲突，不得照其实现"。

### 1.3 模型实际产出的修改

模型返回了 `orderflow/filters.py` 的完整新内容，核心改动为：

```python
+    channel: str | None = None,
...
+    normalized_channel = channel.lower() if channel is not None else None
     for order in orders:
         ...
+        if normalized_channel is not None:
+            order_channel = order.get("channel")
+            if order_channel is None or str(order_channel).lower() != normalized_channel:
+                continue
```

对照本次任务适用的 `SPEC-002@2`（渠道取值含 `mini_program`、比较大小写不敏感、
缺少 `channel` 字段的订单不选中），模型实现与规格逐条一致；
同时 `DECISION-001@1`（精确匹配、区分大小写）已被标记失效，模型**没有**按它实现。

完整差异见 `artifacts/diff.patch`，修改前后快照见 `snapshots/before/` 与 `snapshots/after/`。

---

## 二、数据库（seekdb）使用记录

### 2.1 形态

使用 OceanBase 的 AI 原生检索引擎 **seekdb**，以 `pylibseekdb` **内嵌模式**运行
（`seekdb.open(db_dir=...)`），**不需要部署数据库服务**；数据目录默认
`minimax/.runtime/seekdb`。

### 2.2 实际表与行数（导入后，见 `artifacts/import_report.json`）

| 表 | 含义 | 行数 |
| --- | --- | ---: |
| `spec` | 规格条目与设计决策 | 4 |
| `code_file` | 导入的源文件 | 2 |
| `code_symbol` | 从源码解析出的符号 | 1 |
| `test_case` | 从测试文件解析出的测试用例 | 12 |

其中 `spec` 表里 `status='active'` 3 条、`status='superseded'` 1 条
（`SPEC-002@v1` 被 `SPEC-002@v2` 取代）。

### 2.3 实际执行的查询

导入阶段执行的 SQL 全部记录在 `artifacts/import_report.json` 的 `queries` 字段里，
主要包括：

```sql
DELETE FROM spec; DELETE FROM code_file; DELETE FROM code_symbol; DELETE FROM test_case;
REPLACE INTO spec (...) VALUES (...);                       -- 写入规格条目
UPDATE spec SET status = 'superseded', superseded_by = ?     -- 标记被取代的条目
REPLACE INTO code_file (...) VALUES (...);                  -- 写入源文件
REPLACE INTO code_symbol (...) VALUES (...);                -- 写入符号
REPLACE INTO test_case (...) VALUES (...);                  -- 写入测试用例
```

检索阶段（组装上下文时）执行的查询：

```sql
SELECT spec_id, version, title, kind, status, supersedes, superseded_by, scope, body,
       source_path, content_hash FROM spec ORDER BY spec_id, version;
SELECT path, symbol, kind, signature, body, line_start,
       (CASE WHEN symbol LIKE ? THEN 100 ELSE 0 END
          + CASE WHEN signature LIKE ? THEN 60 ELSE 0 END
          + CASE WHEN body LIKE ? THEN 30 ELSE 0 END) AS score
  FROM code_symbol WHERE symbol LIKE ? OR signature LIKE ? OR body LIKE ? ORDER BY score DESC;
SELECT path, test_name, kind, body,
       (CASE WHEN test_name LIKE ? THEN 100 ELSE 0 END
          + CASE WHEN body LIKE ? THEN 40 ELSE 0 END) AS score
  FROM test_case WHERE test_name LIKE ? OR body LIKE ? ORDER BY score DESC;
```

**这些查询的返回行就是交给 MiniMax 的上下文内容的唯一来源**（`artifacts/context.json`
可逐条比对）：上下文里出现哪一条规格、哪一个符号、哪一个测试，完全由上面的 SQL 决定。

> 说明：`pylibseekdb` 的游标只接受完整 SQL、不支持参数绑定，因此模块把参数渲染为
> 转义后的 SQL 字面量（`_literal` / `_interpolate`，单引号与反斜杠均已转义，
> 单元测试 `test_literal_escaping_is_safe` 覆盖）。

---

## 三、PowerContext 状态写入与复用记录

### 3.1 使用方式

`ctxmod/state.py` 实现 PowerContext 的公开协议，并组合成 PowerContext 的
composition root：

```python
PowerContext(
    sources=Sources(catalog=SourceCatalog(backend=..., adapters=[MaterialSourceAdapter()]),
                    store=SqliteSourceStore(...)),
    artifacts=Artifacts(catalog=SqliteArtifactCatalog(...), store=SqliteArtifactStore(...)),
    triggers=(),
)
```

* 导入的资料 → 注册为 **Source**（`source_type='material'`）；
* 每次保存任务状态 → 写入 **Artifact 的一个新修订**（`family='task-state'`），
  `lineage.sources` 记录该状态引用了哪些 Source。

### 3.2 写入记录

一次 `ctxmod run` 之后的状态（`artifacts/state_after_restart.json`）：

| 字段 | 值 |
| --- | --- |
| `revision` | 1（该任务第 1 次保存） |
| `active_specs` | `["SPEC-001@1", "SPEC-002@2"]` |
| `superseded_specs` | `["DECISION-001@1", "SPEC-002@v1"]` |
| `target_symbols` | `["orderflow/filters.py::filter_orders"]` |
| `test_paths` | `["tests/test_filters_regression.py", "tests/test_filters_target.py"]` |
| `done` | `["修改 orderflow/filters.py 并通过全部测试"]` |
| `todo` | `[]` |
| `validations` | `pytest ... exit=0, 12 passed, 0 failed` |
| `lineage_sources` | `["spec:specs/SPEC-001-order-status.md", "spec:specs/SPEC-002-channel-filter-v2.md"]` |

### 3.3 复用记录（跨进程）

`scripts/run_sample.py` 的第 8、9 步在**全新的 Python 进程**中执行：

```
[ok ] read task state in a fresh process (exit=0)          # ctxmod state --task DEMO-1
[ok ] reuse the stored state to verify the task again (exit=0)  # ctxmod verify --task DEMO-1
```

* `artifacts/state_after_restart.json` 是重启后读回的完整状态；
* `artifacts/verify_after_restart.json` 显示模块读回状态后，用其中的 `done`/`todo`/
  `validations` 再次执行了验证（退出码 0）。

模块自身的单元测试 `test_task_state_survives_a_new_store_instance`
（`tests/test_ctxmod.py`）也验证了"关闭再打开状态库后仍能读到状态"。

### 3.4 状态库形态

`minimax/.runtime/state/powercontext.sqlite3`，两张表：

```sql
CREATE TABLE source (
    source_type TEXT NOT NULL, source_id TEXT NOT NULL,
    payload TEXT NOT NULL, definition TEXT NOT NULL,
    PRIMARY KEY (source_type, source_id)
);
CREATE TABLE artifact (
    family TEXT NOT NULL, artifact_id TEXT NOT NULL, revision INTEGER NOT NULL,
    content TEXT NOT NULL, lineage TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (family, artifact_id, revision)
);
```
