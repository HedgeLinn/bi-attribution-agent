# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 本文件是 `bi-attribution-agent/` 子项目的工作指引。该目录**不是**独立 git 仓库（随上层 `portfolio` 仓库跟踪，嵌套 `.git` 已移除）。
> 恢复工作前先读 **`docs/HANDOFF.md`**（做到哪了 / 下一步 / 怎么验证 / 已知问题），
> 设计与理由看 **`docs/REUSE_DESIGN.md`**（设计的事实来源）；
> 接口字段与签名看 **`docs/ATTRIBUTION_CONTRACT.md`**（契约，双方不得改）。
> 上层 `portfolio/CLAUDE.md` 讲作品集仓库本身与多 agent 协作方法论。

## 项目是什么

对话式 BI **归因**分析 agent —— 不是 Text2SQL。「异常检测 → 假设生成 → 贡献度下钻 → 根因定位」的多轮假设-验证闭环，
产物是带证据链的根因结论，不是一张表。增量价值在**决定下钻哪个维度**，不在把问题翻译成查询。

技术栈：DuckDB + 自建语义层 YAML + 手写 agent loop（LangChain `bind_tools` + while，**不用 LangGraph**）+ Streamlit。

## 常用命令

全部在 `bi-attribution-agent/` 下执行（Windows + PowerShell）。

### 离线验证（不需要任何 API Key）—— 改完必跑

```powershell
# 全套测试：本机 2026-09-16 实测 658 passed（venv，约 36s）
.\.venv\Scripts\python.exe -m pytest -o addopts="" -q

# 只跑一个文件 / 单个用例
.\.venv\Scripts\python.exe -m pytest -o addopts="" tests\test_engine_contribute.py
.\.venv\Scripts\python.exe -m pytest -o addopts="" "tests\test_engine_contribute.py::test_xxx"

# 引擎自测：期望「通过 10 项,失败 0 项」（钉死 ecommerce-demo，多数据集后不再自动选中）
.\.venv\Scripts\python.exe attribution\self_test.py

# 数据集发现：期望 4 个（ecommerce-demo / marketing-funnel / saas-mrr / user-journey）
.\.venv\Scripts\python.exe harness\run.py --list-datasets

# 语义层校验 / 埋点回验（退出码 0 = 通过）
.\.venv\Scripts\python.exe scripts\check_semantic.py --dataset ecommerce-demo
.\.venv\Scripts\python.exe scripts\verify_dataset.py --dataset ecommerce-demo

# 评估脚手架离线自检（mock 剧本；mock 数字是剧本写死的，无模型能力含义，不许引用为准确率）
.\.venv\Scripts\python.exe scripts\evaluate_agent.py --dataset ecommerce-demo --mock
```

- **`-o addopts=""` 必需**：`pytest.ini` 里有 `addopts = -q`，命令行再加 `-q` 会变 `-qq`，连汇总行都不打印。
- **必须用 `.venv`**：Anaconda 全局环境的 streamlit 是 1.45.1，而 `app/attribution_viz.py` 用了 `altair_chart(width="stretch")`（≥1.46 才有），会挂 AppTest 渲染测试。**那是环境问题不是代码 bug —— 不要为它改测试或归因代码**。venv 首次需 `pip install -r requirements.txt pytest`（requirements 只含运行时依赖）。

### 需要模型的入口

```powershell
.\.venv\Scripts\python.exe harness\run.py "为什么 2026 年 6 月 GMV 下滑了？"
.\.venv\Scripts\python.exe harness\run.py --dataset saas-mrr "MRR 为什么掉了？"
.\.venv\Scripts\python.exe scripts\evaluate_agent.py --dataset ecommerce-demo --out reports\<name>.json

.\run.ps1        # 交互式菜单：1 启动 / 2 重启 / 3 停止 / 4 状态 / 5 日志 / 0 退出（PID→.agent.pid，日志→logs/）
# 等价直连：.\.venv\Scripts\python.exe -m streamlit run app\app.py
```

### 造数（离线，固定种子可复现）

```powershell
.\.venv\Scripts\python.exe scripts\generate_data.py          # ecommerce-demo（SEED=42）
.\.venv\Scripts\python.exe scripts\generate_saas.py          # 另有 generate_marketing / generate_userjourney
.\.venv\Scripts\python.exe scripts\profile_data.py --dataset <id>   # 语义层草稿（带 TODO，不是最终地图）
```

## 架构

```
app/app.py (Streamlit)          harness/run.py (CLI)
        ──────────┬──────────────────┘
                   ↓  tools.init_engine(data_dir, semantic_path) 注入全局单例
        harness/loop.py      手写 agent loop：bind_tools + while，最多 20 轮
                   ↓  on_event 回调（tool_call / tool_result / usage / final）——前端据此实时画执行链
        harness/tools.py     5 个模型可见工具，schema 由语义层现场渲染
                   ↓
        attribution/engine.py      AttributionEngine（唯一对外暴露类）
                   ↓
        attribution/sql_source.py  （Repository：SQL 构建与执行）
                   ↓
        DuckDB ← datasets/<id>/data/*.parquet      语义层 ← datasets/<id>/semantic.yaml
```

**五条不可破的层次不变量**（改代码前先自检）：

1. **引擎不认识任何具体数据集**：口径（`expression`）、层级（`hierarchy`）、显示名（`label`/`name_column`）全部来自语义层。
   `tests/test_engine_has_no_hardcoded_vocabulary.py` 用 AST 扫描 `attribution/` 与 `harness/` **机械防回退**（词汇表由 `tests/conftest.py` 从语义层 YAML + 真实 parquet 的列名与实体取值推出，只扫 `datasets/*/semantic.yaml`）——
   **注意 `harness/import_*.py` 六个模块在扫描范围内**：新数据集引入的指标名/列名/维度取值，只允许写进 `datasets/*/semantic.yaml`（或该数据集自己的草稿/清单文件），不许作为字符串字面量出现在 `harness/` 的代码里。
   docstring / 注释里举例允许，**可执行代码里的字符串字面量不行**；`app/` 不在扫描范围。
2. **引擎只返回纯 dict**（JSON 可序列化），不返回 DataFrame 或 DuckDB 对象；`_native`/`_rnd` 负责 numpy 标量与 NaN 归一。
3. **`attribution/` 不认识 SQL 与连接**：取数下沉到 `sql_source.py`，算法层只消费原生数值。
4. **harness 只做参数校验 + 转发**，不做业务计算。
5. **结果必须可复现**：`contribute` / `query_metric` 先按 key 定序再按变化量/变化率排序（并列有兜底），同输入 → 同输出（DuckDB 分组行序不保证稳定）。**改排序逻辑等于改评估基准**。

### 模块分工（单文件 ≤300 行 / 单函数 ≤50 行）

超过 300 行的常规解法是**拆新模块**，先例：`engine_decompose.py`、`semantic_decompose.py`、`harness/tool_catalog.py`、`app/{conclusion,format,history,theme}.py`。
本机实测（2026-09-16）该线附近：`attribution/decompose.py` 与 `semantic_schema.py` **恰好 300 行零余量**，`harness/context.py` 303、`app/attribution_viz.py` 303、`attribution/sql_source.py` 323 已越线。动这些文件前先看 `docs/HANDOFF.md` §4 的行数余量表。

### 提示词与工具描述

- 提示词是**两段式**（`harness/context.py`）：数据集无关的方法论（常量 `_METHODOLOGY`）+ 由语义层渲染的数据集上下文（指标目录含**单位**、维度层级、探测出的时间范围、促销日历、口径陷阱）。
- 模型可见的工具语义写在 docstring 里（`harness/tool_catalog.py` 渲染），**参数说明直接决定下钻正确率**（尤其「过滤用 key 而不是显示名」）。
- **方法论的任何改动都会影响评估成绩**，别顺手改。

## 数据集包与语义层（这张「地图」）

```
datasets/<id>/
├── dataset.yaml        # 清单：id（必须等于目录名）/ title / semantic / data_dir / case_count；`unconfirmed: true` = 按自动地图落位、未经人工确认
├── semantic.yaml       # 语义层 = 归因的地图（schema v2）
├── expectations.yaml   # 埋点黄金断言（verify_dataset.py 回验）
├── cases/*.yaml        # 评估 case（evaluate_agent.py 消费）
└── data/*.parquet

datasets/.pending/<id>/   # 上传暂存区：import_upload 的落点；确认/跳过/超时清理前不出现在数据集发现列表
```

`harness/datasets.py` 的解析优先级：**① 显式 `--data-dir`+`--semantic` → ② `--dataset` / `BI_DATASET` → ③ 磁盘上唯一数据集自动选中 → ④ 报错并列出可选 id**。
③ 刻意为之（只有一个数据集时不该逼用户写 `--dataset`），④ 也是刻意的（多于一个必须显式选，避免「以为跑了 A 实际跑了 B」）。

语义层关键字段（完整样例见 `datasets/ecommerce-demo/semantic.yaml`，规格见 `docs/REUSE_DESIGN.md` §3.2）：
`fact_table` / `date_field` / `metrics.{label,expression,type,time_aggregation,depends_on,unit}` / `dimensions.{table,key,name_column,hierarchy}` / `decompositions[]` / `time.calendar` / `caveats` / `dataset_version`。

- **改地图要留痕**：改 `expression`（口径）/ `time_aggregation` / `hierarchy` 顺序 / 删指标维度 = **破坏性变更**，必须走
  `scripts/check_semantic.py`（退出码 **0** 无变更或仅增量 / **1** 破坏性 / **2** 用法错误）；非 0 时 ① bump `dataset_version` ② 重算受影响 case。
- **前端只读语义层**（`app/semantic_view.py`），不做编辑——编辑会绕过整套治理。
  **唯一的例外是导入后的确认向导**（`app/import_wizard.py`，§6.2 B 档）：上传先落
  `datasets/.pending/<id>/` 暂存区，人工走完 4 步（口径/拆分/恒等式/日历/口径陷阱）再由
  `harness/import_confirm.py` 落位；准入门槛 = 「这份地图从未被人工确认」，一旦确认入口即消失。
  「跳过确认」的包按自动地图落位并标 `unconfirmed: true`，可回来补确认。纯逻辑层在
  `app/wizard_common.py`（含控件键命名表，渲染/记账/回填共用）、`app/wizard_ledger.py`（答案账本 +
  控件原值回填，扛 Streamlit 每 run 清未渲染 widget 的问题）、`app/wizard_steps.py`。
  前沿接口约定:`harness/import_upload` 的返回 dict(`ok/id/title/fact_table/date_field/metrics/dimensions/rows`)是冻结契约,不得改。

## 改代码前必须知道的硬规则

1. **契约先行**：`docs/ATTRIBUTION_CONTRACT.md` 冻结 `AttributionEngine` 的函数签名与返回 dict 的键。语义层字段名同理。**先读契约再改签名/字段名**。
2. **「没有行不等于 0」**（本项目反复出现的判据）：窗口内无数据时如实返回 `None` 或抛 `DecomposeError`，不拿 0 或邻窗冒充。
   异常检测历史不足 → `baseline_type="none"` + `base=None`；语义层 diff 看不出结构 → 标「不可比」而不是「被删除」；找不到真因 → 报「无法归因」。
3. **半可加指标不能跨期求和**（`type: semi_additive` + `time_aggregation: last`，如 MRR / DAU）：整窗、分组、切片一律取窗口内**最后有数据日**的值
   （求和会得到静默 ×N 的错误）。其它 `time_aggregation`（avg / max）尚未实现查询语义，遇到抛 `SemanticError` —— 宁可失败，不静默按 sum 算错。
   多事实表数据集另有约束：`check_reachability` 只校验**默认事实表**的 `date_field` 与维度键。
4. **LMDI 分解的两个警告**（契约 v2，独立审核实测）：① 因子含非正值时走 δ 近似路径，**不满足零残差**（对应 effect 的 label 带「(含非正值,效应为近似)」标注）；
   ② `structural` 的 `total_*` 定义在保留实体上，与 `query_metric` 的标量**数值不同、甚至方向相反**，不可混比、画图不可混画；下架/新上实体单列在 `entity_changes`，看它而不是 effects。
5. **不要绕过防回退测试**，也不要用只跑 happy path 的验证脚本：本工作区**两次**被自己的验证脚本误导过（详见 `docs/HANDOFF.md` §5.5）。

## 环境与依赖的坑

- `streamlit>=1.46` 是硬下限（见上）；`pytest.ini` 的 `addopts` 见上；venv 在 `.gitignore` 里。
- 读无 BOM 的 UTF-8 中文文件用 Read 工具，不要 `Get-Content`（会乱码）。
- `harness/llm.py` 与 `harness/loop.py` **硬编码 `C:/Users/hzl/.claude/skills/llm_cost`** 做记账与成本显示；其它机器上没有该技能时退化为 no-op（成本恒 0），**属预期降级不是 bug**。
- 模型接入按环境变量双协议分派（`harness/llm.py`，**模块加载时求值一次**）：
  设了 `ANTHROPIC_AUTH_TOKEN` → `ChatAnthropic` + `ANTHROPIC_BASE_URL` + 模型 `deepseek-v4-pro-0813`（该常量跟着协议写死）；
  否则 → `ChatOpenAI` + `BI_API_KEY`（必须）+ `BI_API_BASE`（默认 `http://new-api.mypy.cn/v1`）+ `deepseek-v4-flash`。
  两协议都传 `extra_body={"thinking": {"type": "disabled"}}` 关思考（保持 `AIMessage.content` 是纯 str）；`BI_MAX_TOKENS` 默认 8192。
  **代码不加载 `.env`**（`.env.example` 只是变量清单）。本机 `ANTHROPIC_AUTH_TOKEN` 在**机器级**环境变量，`ANTHROPIC_BASE_URL`（用户级）指向 `https://api.deepseek.com/anthropic`，与 `.env.example` 里写的阿里云百炼端点不同。
- 评估要传 `BI_DATASET` 时**传数据集包的绝对根目录**（不是 id），解析失败会抛错而不是静默退化；评分上下文靠环境变量是单线程假设。
- 造数会**覆盖** `datasets/<id>/data/*.parquet`；`preview.html` 是前端设计原型，与 `app.py` 的 CSS 保持一致；`.streamlit/config.toml` 未配端口（run.ps1 回退 8501）。

## 多 agent 协作（本工作区已固化的模式）

上层 `portfolio/CLAUDE.md` 有完整方法论，要点：

- 主 agent 先冻结接口（骨架文件 = 签名 + docstring + `NotImplementedError`），再并行实现。
- **任意两个 subagent 不得共享可写文件** —— 派活前先列文件归属表，`docs/HANDOFF.md` §8.3 是速查表。共享文件（如 `tests/conftest.py`）的禁改要带时限，或由主 agent 自己持有。
- 硬性禁令：subagent 不得跑 git 写命令（只读的 `status`/`diff`/`show` 可以）、不得直接写用户真实数据文件、不得编辑 `docs/`。
- 实现阶段之后配独立**审核**闸门：审核 agent 只读，任务是**证伪**而非复核（自设检查方法 + 受控实验 + 哈希证明已还原），结论要区分「已证实 / 已证伪 / 无法判定」，并单列「未被声明覆盖的风险」。
- 主 agent 必须自己留一部分（集成 + 验收）。

## 文档地图

| 文件 | 用途 |
|---|---|
| `docs/HANDOFF.md` | **交接清单**：做到哪了 / 下一步 / 怎么验证 / 已知问题 / subagent 分工实录——恢复工作的第一入口 |
| `docs/REUSE_DESIGN.md` | 设计的事实来源（目标架构、schema v2、LMDI、评估设计、造数 5 条约束） |
| `docs/ATTRIBUTION_CONTRACT.md` | 引擎与上层冻结的接口合同（字段名与函数签名） |
| `docs/DATA_SCHEMA.md` | 各数据集表结构 |
| `docs/PLAN.md` | 最初的项目方案（2026-09-09，历史背景） |
| `README.md` | 对外文案（作品集视角） |
| `reports/` | 评估报告 JSON（含 live 首跑 `live-ecommerce-2026-09-14.json`，4/9=44.44%；两轮成绩 3/9→4/9 不可复现，引用准确率必须注明批次） |