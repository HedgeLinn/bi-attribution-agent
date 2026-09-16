# BI 归因分析对话 Agent

一个能**主动发现异常 → 提出假设 → 下钻维度 → 定位根因**的 BI 分析 agent。

它不是 Text2SQL。用户问「为什么 GMV 下滑了」，它回的不是一张表，而是一条带证据链的根因结论。

## 为什么不是「查数 agent」

| | 查数 agent（常见做法） | 归因 agent（本项目） |
|---|---|---|
| 触发 | 用户说「查 XX 维度 XX 指标」 | 系统发现异常，或用户问「为什么下滑」 |
| 模型角色 | 翻译（意图 → SQL） | 分析师（提假设 → 验证 → 下钻） |
| 循环 | 单轮问答 | 多轮假设-验证 |
| 产物 | 一张表 | 带证据链的根因结论 |
| 难点 | SQL 准确率 | 语义层 + 归因算法 + **假设质量** |

关键认知：归因 agent 的增量价值是**决定下钻哪个维度**，而不是把问题翻译成查询。

## 归因 loop

```
用户：「为什么 GMV 下滑？」
  → [异常检测]   确认异常：GMV 环比 -15%
  → [假设生成]   LLM 提假设：H1 某地区掉量 / H2 客单价降 / H3 某品类断货
  → [贡献度下钻] 对假设维度算贡献度，取 top-k 切片
  → [验证/排除]  是华东？下钻华东 → 是上海？下钻上海 → 是某门店？
  → [根因确认]   证据链完整
  → [结构化输出] 结论 + 证据链 + 建议
```

## 架构

```
app/app.py  (Streamlit)        harness/run.py  (CLI)
        └──────────┬──────────────────┘
                   ↓
        harness/loop.py          手写 agent loop（LangChain bind_tools + while，最多 20 轮）
                   ↓  on_event 回调（前端实时展示执行链）
        harness/tools.py         5 个模型可见工具
                   ↓
        attribution/engine.py    AttributionEngine（唯一对外暴露类）
                   ↓
        DuckDB  ←  datasets/<id>/data/*.parquet   语义层 ←  datasets/<id>/semantic.yaml
```

```
bi-attribution-agent/
├── docs/                   PLAN.md · DATA_SCHEMA.md · ATTRIBUTION_CONTRACT.md
├── datasets/               数据集包：换数据 = 换目录（dataset.yaml + semantic.yaml + data/）
│   └── ecommerce-demo/     零售电商归因：语义层 + 星型模型示例数据（1 事实表 + 4 维度表）
├── attribution/            归因引擎（贡献度下钻 + 异常检测）
├── harness/                agent loop · 模型接入 · 工具层 · 数据集解析（datasets.py）
├── app/app.py              Streamlit 前端
├── scripts/                造数与数据校验
└── preview.html            前端设计原型（与 app.py 的 CSS 保持一致）
```

**五个工具**（模型据此决策，docstring 即 tool schema）：

| 工具 | 作用 |
|------|------|
| `get_semantic_overview` | 返回可用指标、维度、每个维度的下钻层级 |
| `detect_anomaly` | 判断指标是否真的发生了显著变化 |
| `contribute` | **核心**：对某维度做贡献度下钻，找出最拖累的切片 |
| `query_metric` | 查派生指标（客单价/退款率/优惠率），区分「量掉了 / 价掉了 / 退款暴涨」 |
| `decompose` | 对指标做量级/结构分解（零残差），回答「量变 / 价变 / 结构变」 |

## 快速开始

```bash
pip install -r requirements.txt

# 配置模型 API Key（不配则 agent 无法运行；离线自测不受影响）
# Bash:
export BI_API_KEY=sk-xxx
# PowerShell:
$env:BI_API_KEY = "sk-xxx"

# 跑一轮归因（在项目根目录执行；磁盘上只有一个数据集时不需要 --dataset）
python harness/run.py "为什么 2026 年 6 月 GMV 下滑了？"

# 换数据集：--dataset <id> 或环境变量 BI_DATASET；--data-dir/--semantic 可自带数据
python harness/run.py --dataset ecommerce-demo "为什么 2026 年 6 月 GMV 下滑了？"
python harness/run.py --list-datasets          # 列出可用数据集（不需要 API Key）

# Web 前端
streamlit run app/app.py
```

> API Key 只从环境变量读取，代码中不硬编码。`.env.example` 是变量清单，供你自己在 shell 或 IDE 里配置。

**离线自测**（不需要 API Key，验证引擎与数据）：

```bash
python attribution/self_test.py   # 引擎自测：查数 / 贡献度下钻 / 异常检测
python scripts/verify_data.py     # 数据校验
python scripts/generate_data.py   # 重新造数（固定随机种子，结果可复现）
```

**环境变量**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BI_API_KEY` | 空 | 模型 API Key，未设置时无法调用 LLM |
| `BI_API_BASE` | `http://new-api.mypy.cn/v1` | 网关地址 |

模型 `deepseek-v4-flash`，temperature=0（分析任务要稳定可复现），显式关闭思考模式以加快 tool calling。

## 数据里埋了一个「标准答案」

示例数据是刻意设计的，用来检验 agent 是否真的会下钻：

- **主异常**：华东 → 上海 → `STORE_S0001`（上海徐家汇旗舰店）自 2026-06 断崖下跌。
  根因 = 该店头部 SKU `SKU_P0001`（旗舰智能手机 Pro Max，8999 元，占该店 GMV 约 55%）下架
  → 客单价 -50%、销量 -13.6%、GMV -57.4%。
- **干扰项**：同期 618 大促后全量自然回落，**所有**区域都在跌。

干扰项的作用是逼迫 agent 不能停在「整体下跌」就下结论，必须一路下钻到门店/SKU 层才能定位真凶。

## 设计要点

- **手写 agent loop，不依赖 LangGraph。** `harness/loop.py` 就是一个 `while` + `bind_tools`，
  目的是把「感知 → 决策 → 行动 → 观察」这条循环跑在明处，而不是被编排框架遮蔽。
- **契约先行。** `docs/ATTRIBUTION_CONTRACT.md` 是引擎与上层之间冻结的接口合同（字段名与函数签名双方不得改动），
  `datasets/<id>/semantic.yaml` 是语义层契约。引擎全部方法只返回纯 dict（可 JSON 序列化），不泄漏 DuckDB 内部对象。
- **additive vs derived 分开处理。** gmv/sales_qty/orders_count 可加法分解，贡献度 = `change / total_change`；
  aov/refund_rate/discount_rate 无法加法分解，退化为按变化率排序。
- **模型可见的工具语义写在 docstring 里。** 工具参数说明（尤其是「过滤要用 ID 而不是显示名」）直接决定模型的下钻正确率。
