# 规有所依 —— 面向软件持续演进的 SDD 上下文管理系统

本目录是 MiniMax 赛道的参赛材料：为一个已有的 AI 开发助手（**PowerContext**）实现**工程上下文模块**，
并用一次**真实的代码修改**验证模块的作用。

模块解决的是题目描述的问题：开发助手在持续演进的工程里，需要知道**当前该遵守哪些规格**、
**哪些决策已经失效**、**该改哪些代码**、**用哪些测试验证**，以及**上一个会话做到哪一步了**。

---

## 1. 目录结构

```
minimax/
├── README.md                 本文件：运行说明
├── TECH_NOTES.md             一页技术说明：模块结构 / 上下文选择方式 / 技术分工
├── TECH_USAGE_LOG.md         技术使用记录：MiniMax、seekdb、PowerContext 的实际调用记录
├── requirements.txt          依赖与版本
├── ctxmod/                   上下文模块源码
│   ├── config.py             运行配置（路径、数据库名、模型）
│   ├── db.py                 seekdb 资料库：规格 / 代码 / 测试的存取与检索
│   ├── specs.py              规格与代码解析（front matter + ast）
│   ├── state.py              PowerContext 任务状态：Source / Artifact 存取
│   ├── context.py            开发上下文组装（适用规格、失效决策、代码、测试）
│   ├── agent.py              MiniMax 调用桥接
│   ├── patch.py              代码块提取与差异生成
│   └── cli.py                命令行入口
├── demo/                     业务工程（被修改对象）
│   ├── orderflow/filters.py  待修改的业务代码
│   ├── specs/                规格与设计决策（含一个已作废版本）
│   └── tests/                固定测试集（目标功能测试 + 回归测试）
├── snapshots/
│   ├── before/               修改前的代码快照
│   └── after/                修改后的代码快照
├── scripts/run_sample.py     验证样例驱动脚本（一条命令跑完整流程）
├── tests/test_ctxmod.py      模块自身的单元测试
├── examples/                 验证样例说明
└── artifacts/                运行产物：上下文、提示词、回答、差异、测试日志、状态、报告
```

---

## 2. 依赖版本

| 组件 | 版本 | 作用 |
| --- | --- | --- |
| Python | ≥ 3.11（实测 3.12 / 3.14） | 运行时 |
| pylibseekdb | 1.4.0.post2 | 内嵌 OceanBase **seekdb** 引擎，免部署数据库服务 |
| powercontext | 1.4.1 | PowerContext 产品本体，用于保存/复用任务状态 |
| pydantic | 2.14.2 | powercontext 依赖 |
| PyMySQL | 1.2.3 | pylibseekdb 依赖 |
| pytest | ≥ 8 | 运行测试 |

安装：

```bash
pip install -r requirements.txt
# 网络较慢时可用国内镜像
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

> `pylibseekdb` 官方只在 Linux/macOS 标注了平台，但 1.4.0.post2 同时发布了
> `cp312-abi3-win_amd64` wheel，本材料在 **Windows 11 + Python 3.14** 上验证通过。

---

## 3. 配置

| 环境变量 | 说明 | 默认值 |
| --- | --- | --- |
| `MINIMAX_API_KEY` | MiniMax API Key（公网 `api.minimax.cn`） | 未设置 |
| `MINIMAX_BASE_URL` | MiniMax 接口地址 | 取自 `~/.minimax/config.yaml` |
| `MINIMAX_CONFIG` | 配置/凭据文件目录 | `~/.minimax` |
| `CTXMOD_SEEKDB_DIR` | seekdb 数据目录 | `minimax/.runtime/seekdb` |
| `CTXMOD_STATE_DB` | PowerContext 状态库 | `minimax/.runtime/state/powercontext.sqlite3` |
| `CTXMOD_LOG_DIR` | 日志目录 | `minimax/logs` |
| `CTXMOD_MINIMAX_MODEL` | 使用的模型 | `MiniMax-M3` |

**凭据来源优先级**：`MINIMAX_API_KEY` → `~/.minimax/config.yaml` 中 provider 的 `apiKey`
→ 已登录 MiniMax Code 时的 `~/.minimax/auth/**/auth.json` 里的 `accessToken`。
本材料默认使用第三种（随账号登录态自动可用），因此开箱即可运行。

> 配置示例只使用占位值，仓库内不含任何真实凭据。

---

## 4. 数据初始化

```bash
cd minimax
python -m ctxmod init      # 建目录、建 seekdb 库、建 PowerContext 状态库
python -m ctxmod import    # 导入 demo 的规格 / 代码 / 测试
```

`import` 会做两件事：

1. 把规格、代码文件、代码符号、测试用例写入 **seekdb**（`spec` / `code_file` / `code_symbol` / `test_case` 四张表）；
2. 把每一份资料注册为 **PowerContext Source**，作为后续任务状态的 provenance。

导入时会解析规格 front matter 的 `supersedes` 字段，把被取代的条目在库中标记为
`superseded`，并记录被谁取代。

---

## 5. 调用入口

模块入口是命令行（CLI）：

```bash
python -m ctxmod init
python -m ctxmod import      --project demo
python -m ctxmod context    --task DEMO-1 --description "<任务描述>" --target orderflow/filters.py [--json]
python -m ctxmod run        --task DEMO-1 --description "<任务描述>" --target orderflow/filters.py [--model MiniMax-M3] [--no-apply]
python -m ctxmod state      --task DEMO-1
python -m ctxmod verify     --task DEMO-1
python -m ctxmod reset
```

**输入 / 输出**

| 命令 | 输入 | 输出 |
| --- | --- | --- |
| `import` | `--project` 工程目录 | 导入报告（各表行数、PowerContext 资料数、实际执行的 SQL） |
| `context` | `--task` `--description` `--target` | 开发上下文：适用规格条目、已失效决策、需修改的代码、验证用测试、已有任务状态。`--json` 输出结构化结果 |
| `run` | 同上 + `--model` | 组装上下文 → 调用 MiniMax → 落盘修改 → 跑测试 → 写状态；同时产出 `artifacts/` 下的提示词、回答、差异、日志 |
| `state` | `--task` | PowerContext 中该任务的状态与修订历史 |
| `verify` | `--task` | 用已保存的状态再次验证（进程重启后仍可复用） |
| `reset` | — | 删除 `.runtime/`，回到空状态 |

---

## 6. 测试命令

```bash
# 模块自身单元测试
python -m pytest tests -q

# 业务工程固定测试集（目标功能测试 + 回归测试）
python -m pytest demo/tests -q --tb=short

# 一条命令跑完整验证样例（含重启、规格演进、产物记录）
python scripts/run_sample.py
```

`scripts/run_sample.py` 会依次执行：恢复修改前快照 → 清空运行时 → 规格演进对照 →
重新导入 → **修改前跑测试（预期失败）** → 组装上下文 → **MiniMax 修改代码** →
**修改后跑测试（预期全部通过）** → **新进程读回任务状态** → 复用状态再次验证，
并把每一步的命令、退出码写进 `artifacts/REPORT.md`。

---

## 7. 验证样例

见 `examples/README.md`（任务描述、有效规格、初始数据、操作步骤、预期结果、重置方法）。

---

## 8. 已知限制

1. **MiniMax 凭据**：默认使用 MiniMax Code 的登录态 token。该 token 过期后需重新登录，
   或改用 `MINIMAX_API_KEY` 公网 Key。
2. **Windows 上的 seekdb**：内嵌 seekdb 在 Windows 下通过本地回环端口提供服务，
   进程退出瞬间日志文件可能仍被占用（仅影响临时目录清理，不影响功能）。
3. **检索方式**：当前使用 SQL `LIKE` 打分检索（关键词 + 中文二元组），未启用向量检索；
   接入 embedding 后可平滑替换 `MaterialStore.search_*`。
4. **关键字提取**：中文按二元组切分，英文按词切分；关键词很多时按出现顺序截断。
5. **模型输出解析**：约定模型在 ```python 代码块中返回目标文件的完整内容；
   若模型返回 diff，模块不会自动应用。
6. **单机形态**：上下文状态存在本地 SQLite，未做多用户隔离与并发写入优化。

---

## 9. 复现所需环境

* 已安装 Python 3.12+；
* 可访问 MiniMax 接口；
* 无需 Docker、无需外部数据库服务（seekdb 以内嵌库方式运行）。
