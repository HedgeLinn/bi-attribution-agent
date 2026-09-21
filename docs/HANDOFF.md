# 交接清单（HANDOFF）

> 最后更新：2026-09-13
> **用途**：会话中断后**从这里恢复**。本文件只讲「做到哪了 / 下一步做什么 / 怎么验证」；
> 设计与理由看 `docs/REUSE_DESIGN.md`（那是设计的事实来源）。

---

## 0. 三十秒上手

```powershell
cd E:\hzl_project\portfolio\bi-attribution-agent
pip install -r requirements.txt

python -m pytest -o addopts="" -q          # 期望：561 passed, 0 failed
python attribution/self_test.py            # 期望：通过 10 项,失败 0 项
python harness/run.py --list-datasets      # 期望：ecommerce-demo、saas-mrr、marketing-funnel、user-journey 四个数据集
python scripts/check_semantic.py --dataset ecommerce-demo   # 期望：语义层校验通过
```

> **加 `-o addopts=""`**：`pytest.ini` 里有 `addopts = -q`，命令行再加 `-q` 会变成 `-qq`，
> 那样连汇总行都不打印，看不出通过数。

跑完整 agent（会调 LLM）需要 `ANTHROPIC_AUTH_TOKEN` 或 `BI_API_KEY`（`harness/llm.py` 双协议分派：有前者走 Anthropic 协议/阿里云百炼 token-plan 端点/`deepseek-v4-pro-0813`，否则走 OpenAI 协议/`BI_API_BASE`/`deepseek-v4-flash`）。本机（desktop-34hbi44）`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL` 已持久化在 Windows 用户环境变量。
不带 key 也能验证的入口：`--list-datasets`、`self_test.py`、`check_semantic.py`、`verify_dataset.py`、`evaluate --mock`、全部 pytest。

---

## 1. 项目是什么

对话式 BI **归因**分析 agent——不是 Text2SQL。
「异常检测 → 假设生成 → 贡献度下钻 → 根因定位」的多轮假设-验证闭环。

- 语义层（`semantic.yaml`）是**下钻归因的地图**：定义哪些指标可归因、每个维度能沿什么路径下钻
- 引擎（`attribution/`）**不认识任何具体数据集**，口径、层级、显示名列全部来自语义层
- 有**机械防回退测试**保证这条不退化（见 §5）

---

## 2. 已完成

### M1 语义层可执行化 ✅

- `attribution/expression.py` —— 把 YAML 里的 `expression` 编译成 DuckDB SQL（此前 YAML 只是**说明**，引擎走的是写死的 Python dict）
- `attribution/semantic_schema.py` / `semantic.py` —— 地图的定义与自证 / 服务
- `attribution/sql_source.py` —— 数据访问层（Repository）
- 语义层升 **schema v2**：`time_aggregation` / `decompositions` / `name_column` / `time.calendar` / `dataset_version`
- **行为不变性已证明**：`self_test.py` 10/0，且 15/15 切片值与改造前逐一相同

### M2 数据集包化与动态化 ✅

| 内容 | 产物 |
|---|---|
| 数据集包 | `datasets/ecommerce-demo/{dataset.yaml, semantic.yaml, data/*.parquet}`（原 `data/`、`semantic/` 已迁走） |
| 发现与解析 | `harness/datasets.py`（四级优先级：显式路径 > `--dataset`/`BI_DATASET` > **唯一数据集自动选中** > 报错并列出可选 id） |
| CLI | `harness/run.py`：`--dataset` / `--data-dir` / `--semantic` / `--list-datasets` |
| 工具 schema 动态化 | `harness/tools.py`——枚举来自语义层，**改 YAML 即变**（实测：加指标后枚举自动多一项） |
| 提示词拆分 | `harness/context.py`：方法论（常量）+ 数据集上下文（渲染）。日期范围是**探测**的，不再写死 |
| 语义层 diff | `attribution/semantic_diff.py` + `semantic_diff_rules.py` + `scripts/check_semantic.py`（退出码 **0** 无变更/仅增量、**1** 有破坏性、**2** 用法错误） |
| 前端选择器 | `app/dataset_selector.py` + `app/semantic_view.py`（只读视图） |

### M3 分析能力深化 ✅

- ✅ `attribution/decompose.py` —— LMDI 四种分解（乘法 / 加法 / 比率 / 结构），**零残差**（`Σeffect ≡ ΔV`）
- ✅ `attribution/anomaly.py` —— 日历感知异常检测。实测：618 场景下**传日历 → 不报异常；不传 → 误报**
- ✅ **接线进引擎**（2026-09-13 完成，见 §3 已勾销的 M3-a）：
  - `engine.detect_anomaly` → 日历感知：日序列 + 同星期几稳健基线 + `baseline_type`/`is_expected`/`anomaly_kind`（契约 v2，只增不改）
  - `engine.decompose` → 声明驱动（语义层 `decompositions`），四种 kind 整窗 + 切片形态，零残差
  - 语义层解析 `decompositions` / `time.calendar`（`attribution/semantic_decompose.py` 新模块）
  - **B 案例裁决**：618 干扰项「不再误报」在 1× 回看口径下不成立（促销剔除掏空 4 周干净基线 → 退化 flat 基线虚高 → −16.6% 误报）。**裁决：序列回看 = 估计窗口 × 2**（56 天），B 案例成立（−13.7%），A 案例不受影响。绊线测试钉在 `tests/test_engine_anomaly.py`。

---

## 3. 待完成清单

> 每项都给了「目标 / 涉及文件 / 验收标准」。设计细节见 `REUSE_DESIGN.md` 对应章节。

### M5-saas-mrr ✅ 数据集落地(2026-09-13)+ 设计验证结论

- 交付:`scripts/generate_saas.py` + `datasets/saas-mrr/` 全包(4 表/80 账户/162 变动行,整数分,构造性对账)+ 14 条回验全过 + 4 case + `tests/test_saas_dataset.py`(11 用例)
- **设计验证暴露 4 个真缺口,已修**:
  1. `unit` 解析了但渲染层不输出(模型会把分当元,差 100 倍)→ `render_overview` 与 `render_metric_catalog` 都带上单位
  2. 半可加切片各取自己的末日 → Σ切片≠总量 → **统一基准日**(窗口内全局最后有数据日,QUALIFY 改 CTE ref-day),切片与聚合透镜自洽
  3. `contribute` 对半可加走比率分支(贡献度恒 None)→ 改走加法分支(基准日水平可加)
  4. expectations 缺「流出现」检查 → 新增 `type: appeared`(基准期无值、出现期有正值)
- **对后三套数据集的硬约束(记住)**:①加法分解要求数据起点 = 水平零点(账本不能截断),`decompose` 必须写成「起点→基期末」对「起点→对比期末」的桥式;②默认事实表必须含 date_field 与维度键(check_reachability 只查它);③半可加指标的加法分解**只支持整窗桥式**,切片形态会被恒等式校验拦下(DecomposeError 是诚实行为);④金额用整数分,浮点对账会失守;⑤`self_test.py`/`verify_data.py` 已钉死 ecommerce-demo(多数据集后不再自动选中)

### ~~M5-P0 引擎扩展：半可加 + 多事实表~~ ✅ 已完成(2026-09-13)

- `attribution/sql_source.py`（300 行）：半可加(`time_aggregation: last`)分派——整窗/分组/切片取**末日值**（QUALIFY 按日期倒序取组内第 1 行），非 last 取值抛 SemanticError；多事实表(`metric.source`)分派——`fact_for` 决定 FROM 的表，日期字段约定同名
- 契约 v2.1：半可加语义表（见 `ATTRIBUTION_CONTRACT.md`）
- 测试：`tests/test_semi_additive.py` 8 用例（末日值语义、分组、切片、日序列、未支持取值、行为不变、多事实表）
- **已知缺口（记录）**：`check_reachability` 只校验**默认事实表**的 date_field 与维度键——多事实表数据集的默认表必须含这些列（saas-mrr 的 subscriptions 即默认表），source 表的列存在性由表达式编译期保证

### ~~M3-a 把分解与异常接线进引擎~~ ✅ 已完成(2026-09-13)

### ~~M3-b 归因树 + 瀑布图~~ ✅ 已完成(2026-09-13)

- 新模块 `app/attribution_viz.py`（恰 300 行）：纯数据函数（不 import streamlit）+ 渲染函数（`st.graphviz_chart` 树 / `st.altair_chart` 瀑布图，**零新依赖**）
- `app/app.py` 只加 3 行挂接：`render_from_events(st, events)`
- `tests/test_attribution_viz.py` 10 用例（含 AppTest 真渲染）；真实事件流冒烟：树 7 节点/6 边、Σdelta ≡ total_change 精确为 0
- **审核后修订**（审核证伪 2 项 + 9 风险，8 项裁定全落地）：失败 dict `{"error":...}` 如实呈现（⚠️ + 工具报错原文，不再说假「比率型」）；成链规则改为同维度+同指标+层级加深（`level_orders` 可选参数，app.py 已传语义层层级序）；`use_container_width`→`width="stretch"`；多 anomaly 各起一棵树；维度节点带指标名；nan/inf 不产假标签、不入瀑布数值；瀑布图去 stack 改 y0/y1 显式计算（负累计也正确）；「其余切片」三态分开标注。**339 → 338 passed**（10→9 用例合并，行数约束）
- 剩余已知（见 §4）：未知形态结果静默不画（无提示）；contribute 工具无 filters → 「filters 命中切片」挂法仍只在单测里走到；decompose 未接线为工具

### ~~M4 评估脚手架~~ ✅ 已完成(2026-09-13)

- `harness/loop.py::_finalize`：结论 JSON 校验 + 一次有界重试（网关抖动返回原文，宁缺毋崩；30 测试 + 8/8 变异击杀）
- `harness/context.py` 方法论：最终输出格式升级为 §5.8 结构化（根因/量级/已排除/无法归因/置信度）
- `scripts/evaluate_agent.py`（+ eval_common / eval_mock_scripts / eval_report 三个配套模块）：case 加载（坏 case 抛错不跳过）、五维机械评分（无法归因类 case 定位维改判诚实度）、mock 剧本（6 种判定路径有区分度）、分组准确率报告
- `scripts/verify_dataset.py`（+ verify_checks）：expectations 回验（metric_change / slice_absent），**3/3 全过**（真实数值：门店断崖 -57.40%、SKU_P0001 下架零销量、promo 噪声 -67.87%）
- `datasets/ecommerce-demo/cases/c1..c4.yaml`：真实数值区间（引擎现算）。**c4 题目替换**（裁决已接受）：原「2026-07 环比 06」数据上是 +0.20%（6 月被断崖压低），改为 2025-12→2026-01 季节性低谷（-26.05%，检测器报异常但无切片根因——更有张力的诚实度 case），偏差记入 case note
- **mock 报告**：4 case 通过 2，准确率 50%（剧本值，无模型能力含义——真跑需 BI_API_KEY，本机未设）
- **配套模块拆分**（裁决已接受）：`eval_common.py`/`eval_mock_scripts.py`/`eval_report.py`/`verify_checks.py`——300 行约束下冻结契约本身已占约 210 行，拆分是唯一解（先例：`engine_decompose.py`）
- 退出码口径（裁决）：evaluate_agent 0 报告产出 / 1 运行失败 / 2 用法错误；verify_dataset 0 全通过 / 1 有未通过 / 2 用法错误
- **审核后修订**（审核证伪 5 项，7 项裁定全落地，**447 passed**）：①深度维环境依赖修复（`BI_DATASET` 传数据集包**绝对根目录**而非 id，解析失败**抛错**不再静默退化；仓库外数据集与仓库内结果逐值一致有测试钉死）②空 cases 目录/`limit<=0`/过滤后为空一律抛错（0 分母绿报告封死）；报告新增 `limit`/`filters` 键自证分母 ③mock 剧本按出厂 case id 显式绑定（c1 全对/c2 三错/c3 撞词/c4 诚实通过），四维各有失败样本（变异实验逐字节证明，还原 sha256 已证）④`_REQUIRED_FIELDS` 落地为字段→维映射（缺字段按对应维不通过；证据链只记 missing_fields）⑤无法归因与根因互斥（null-root case 带假根因 → 定位不通过）⑥`--case/--category/--tier` 过滤器（**`--sample` 裁决延后**，未实现）⑦expectations.yaml 缺失抛错（不再 0/0 全绿）

### M5 数据集建设（四套，共 30 个 case）

**顺序建议**：`saas-mrr` → `marketing-funnel` → `user-journey`
（先做最小的一套验证 schema v2 的新表达能力设计对不对，假设错了此时改最便宜）

| 数据集 | case 数 | 主考 | 关键点 |
|---|---|---|---|
| `ecommerce-demo` | 6 | 量价结构 | 扩到 6 个 case（现有 1 埋点 + 1 干扰项要整理） |
| `saas-mrr` | 4 | **加法分解** + 半可加 | MRR 是**半可加**指标，跨期不可求和（§1.3 的静默错误） |
| `marketing-funnel` | 14 | 数据质量 + 漏斗 | 8 张表，平台表/埋点表/订单表**三分离**；6 个坑见 §5.4 |
| `user-journey` | 6 | 跨粒度 + 同期群 | 跨粒度比率走 `two_stage`；U4 是「ARPU 上升实为结构效应」 |

- **规格**：`REUSE_DESIGN.md` §5（每套的表结构、埋点清单、评分设计、造数 5 条约束）
- **验收**：
  - **埋点回验脚本**能从数据里反向检出每个坑
  - **切片×时间隔离检查**：任意两个 case 的影响集合不相交
  - 半可加指标**不得被时间求和**

### M5-user-journey ✅ 数据集落地(2026-09-13)

- 交付:`scripts/generate_userjourney.py` + `datasets/user-journey/` 全包(events 33.9 万行/14,558 用户/6 case/27 条回验)+ `tests/test_userjourney_dataset.py`。验收:484 passed、check_semantic 过、回验 27/27、self_test 10/0
- 两处防撞名改名(词汇不变量逼的):指标 `signups`→`registrations`、事件值 `visit`→`app_open`(与引擎标识符/合成语义层撞名),数值逐值不变
- 三个引擎能力首次被真实数据集使用:派生维度(cohort)、半可加 DAU(U5 双口径陷阱:窗口末值 +2.26% / 逐日求和 -2.99% / 去重 -3.51%,三个口径三个答案)、structural 分解(U4 反向辛普森:结构效应 +113.3 分/151.76% 主导,残差 4e-13)
- 已知(记录):U4 的 decompose 未接线为工具,agent 算不出结构效应——case 用定位+深度+断言三维评分;U1 影响集余量最薄(冠军/亚军 1.58,重造可能漂);U5/U6 的 null-root 是主动扩展(口径题/数据质量题的正确根因都是「无业务切片」)
- **数据集验证标准(用户 2026-09-13 裁决)**:字段对、文件对、有内容 + 自动入口(check_semantic / verify_dataset / pytest / self_test / evaluate --mock)全绿即算完成——**数据集类不再安排独立审核闸门**;引擎/评分代码类里程碑维持审核强度

### ~~M5 审核闸门~~ ❌ 取消(用户裁决:数据集验证按自动入口全绿即过)

### M5-ecommerce-demo ✅ 扩 case 完成(2026-09-13)——M5 整体收口

- 交付:`generate_data.py` 拆分(298 行,既有逻辑+尾部调用场景模块)+ `generate_data_scenarios.py`(280 行,E1 纯量/E2 纯价/E4 量价反向/E5 门店结构/E6 隐性价降)+ 5 个新 case + expectations 3→13 条 + `tests/test_ecommerce_expansion.py`(16 用例)
- **钉死数值零漂移**:301 键采样(相对容差 1e-9)只有 7 个预期键变化;2026-06/2025-12/2026-01/STORE_S0001/SKU_P0001 一个没动;拆分本身 md5 逐表证明零行为改变(monkeypatch 掉场景模块后与拆分前逐表相同)
- **主动偏离(已接受)**:改了 7 处既有测试的计数/ID 断言(评估分母 4→9 的机械更新,断言逻辑未动)
- **M5 总计**:四套数据集 33 个 case(9+4+14+6,计划 30——M4 脚手架的 c1/c3/c4 与 E 系列并存所致,记入偏差),回验 89 条全过,`pytest 502 passed`
- 已知(记录):E1/E2 的 contribution_range 用全量口径(城市口径不可辩护);E5 只开 1 家新店(评分是单键严格相等,多家店无法单键表达);E5 新店商品结构是复制来的;新坑只覆盖 2025-04/07/08 三个窗口;mock 通过率与 case 质量无关(真实可解性要等有 Key 的 live 跑)

### ~~M6 语义收集~~ ✅ 已完成(2026-09-13)——方案全部落地

- **A 档** `attribution/profile.py` + `scripts/profile_data.py`(23 用例):表统计 -> 语义层草稿。实测:saas-mrr 的 `mrr_amount` 被正确识别为**半可加候选**(§6.1 点名此工具的首要理由),ecommerce 的维度层级草稿与真值**逐字一致**
- **C 档** `attribution/annotations.py` + `harness/context.py` 回注(36 用例):annotations.jsonl 原子读写、CJK 二字词相关性选择(单字切分会把「月」误注入,已修)、`render_system_prompt(..., query=None)` 选择性注入(query=None 行为逐字不变,既有 33 测试为回归证据)
- **写入侧(补记,2026-09-14 接线)** ✅:M6 遗留的「run 结束时沉淀结论」已落地——`loop.run(persist=True)` 两处出口把结构化结论追加进 `datasets/<id>/annotations.jsonl`(「已排除」-> ruled_out、「证据链」-> evidence,从 confirmed 弹出;非 JSON 不沉淀);评估路径 `run_live` 显式 `persist=False`(批量跑分不污染知识库)
- **M6 语义收集 + B 档人工确认向导**:上传不再直接产出正式数据集 —— `harness/importer.py` 的
  `import_upload` 落 `datasets/.pending/<id>/`(暂存区,超时自动清理,不出现在数据集发现列表),
  `app/` 四步确认向导(① 事实表与时间轴 ② 指标口径 ③ 关系与单位 ④ 世界知识)收齐答案后由
  `harness/import_confirm.py` 的 `commit`/`skip`/`reconfirm` 落位:写盘前两道闸门
  (assert_valid 地图自洽 + 真引擎逐指标冒烟),清单最后写。「跳过」的包按自动地图落位并标
  `unconfirmed: true`(版本仍是 "import"),选择器显示「(未确认)」后缀,可随时回来「补确认」。
  UI 与逻辑分层:`app/wizard_common.py`(纯逻辑+控件键命名表)/ `app/wizard_steps.py`(四步控件)/
  `app/wizard_ledger.py`(答案账本 + 控件原值回填,扛 Streamlit 每 run 清 widget 的问题)/
  `app/import_wizard.py`(状态机+落位)。**「前端只读语义层」表述不适用于这段流程**:它管的是
  「地图的诞生」而非「地图的修改」,准入门槛 = 从未人工确认;一旦确认入口即消失(见 REUSE_DESIGN §6.2 B 档)。
- 已知边界(记录):单调性阈值 0.90 余量薄;外键只用了必要条件;事实表判据是「行数最多」;指标草稿只覆盖数值列→SUM;日期维度表的 year/month 层级会丢;`--dataset` 不带 `--out` 会写进数据集包;context.py 恰 300 行零余量;`harness/import_*.py`(import_answers/import_confirm/import_draft/import_metric/import_pending)在防回退词汇测试的 `harness/` 扫描范围内,新数据集词汇(指标名/列名/实体取值)只允许出现在 `datasets/*/semantic.yaml`

### M7(可选)交叉维度 2D 分解 —— 未做(成本明显高于其他项,见 REUSE_DESIGN §4.5)


- `attribution/profile.py` + `scripts/profile_data.py` —— 从数据生成语义层草稿，
  **含半可加候选识别**（§6.1）
- `attribution/annotations.py` + `annotations.jsonl` —— 归因结论沉淀与**选择性**注入（§6.3）

### M7（可选）交叉维度 2D 分解

见 `REUSE_DESIGN.md` §4.5。成本明显高于其他项。

### 模型双协议接入 + 评估 live 首跑 ✅（2026-09-14）

- `harness/llm.py` **双协议分派**：设了 `ANTHROPIC_AUTH_TOKEN` 走 ChatAnthropic（阿里云百炼 token-plan 端点，`ANTHROPIC_BASE_URL`；模型 `deepseek-v4-pro-0813`，该端点目前唯一模型），否则维持 OpenAI 协议原路径（`BI_API_KEY`/`BI_API_BASE`/`deepseek-v4-flash`）。两协议共用 `extra_body={"thinking": {"type": "disabled"}}`（关思考——保持 `AIMessage.content` 纯 str，实测已生效）；模型默认值随协议分派，`BI_MAX_TOKENS`（默认 8192）可调。`langchain-anthropic>=0.3` 入 requirements
- `scripts/eval_report.py::require_api_key` 改为两协议任一凭证放行；对应测试删双 Key（作者机器有持久的 `ANTHROPIC_AUTH_TOKEN`，只删一个会被放行）
- **评估真跑路径首修**（§4 换行闭环）：`evaluate()` 真跑分支补 `tools.init_engine(data_dir, semantic_path)`（延迟 import，mock/离线路径不碰 LLM 依赖）——此前评估绕开 CLI/前端直接进 loop，全局引擎单例未注入，首跑即报「语义层未初始化」；`--out` 写盘失败退出码改 1（原误走 2 用法错误）
- **live 首跑报告** `reports/live-ecommerce-2026-09-14.json`（未入库）：ecommerce-demo 9 case **4 过，准确率 44.44%**（L1 1/1、L2 3/7、L3 0/1；合计约 ￥2.4，6–12 轮/case）。通过 c4/e1/e2/e5；失败 5 例中 c2 只挂「量级.贡献度为 null」，其余挂定位/深度——**模型结论语义基本正确**（c1 挖到真机制 SKU_P0001 下架、e6 挖到粮油调味+男装优惠率翻倍），输在评分是**单键严格相等**而模型的键/层级与 case 期望切片不同，「c1 contribution_range 偏宽」一行的旧文案首次实证
- **两轮成绩不可复现**（3/9 → 4/9，c4 由败转胜）：R3（contribute top 排序并列无兜底）有了真实证据，引用准确率数字必须注明批次

---

### M8 图表持久化 + 图型扩充 ✅（2026-09-20）

**问题**：图表是**一次性**的 —— 画图代码在 `if prompt:` 分支里（`app/app.py`），而 `st.chat_input`
的值只在那一次 run 有效，所以下一轮**任何**交互（切会话、刷新、点按钮）图就没了；写历史时也不存
事件流，回放路径只有文本。同时图型只覆盖「归因」半边：`query_metric`（最像传统 BI 查数的工具）
连一张柱状图都画不出来，比率型指标直接「不画」。

**改动**：

- **留存**：assistant 消息 payload 增 `id` + `events`（紧凑 JSON 串，只留 `tool_call`/`tool_result`）；
  回放时 `render_from_events` 用**同一段渲染代码**重建图 → 切会话 / 刷新后图仍在
- **新图型（2 → 8 种）**：BI 基础三件（柱状 / 折线 / 表格，`query_metric` 驱动）+ 基线对比
  （`detect_anomaly` 的 base / cmp ±MAD）+ 变化率条形（比率型 —— **从「不画」改成画它能诚实支撑的东西**）
- **图型切换器**：查数结果挂 `st.segmented_control`，用户点选换图型（只换渲染，不重取数、不花模型钱）
- **分级渲染**：步数超 `chart_plan.CHART_LIMIT` 时只画最近的 + 一个展开按钮（**不砍图**；
  streamlit 是命令式执行，未渲染的分支零前端开销）
- **模块拆分**（压回 300 行红线）：`app/attribution_viz.py` 303 → 257 行，新增
  `chart_data`（纯数据 builder）/ `chart_theme`（配色 token）/ `chart_plan`（键名 + 折叠计划）/
  `chart_basic` / `chart_attr`（渲染）
- **顺带修掉一个既有数据安全 bug**：`_save_history` 旧实现是 `open("w")` **先截断**再 `json.dump`，
  payload 含不可序列化对象时整份聊天历史会被清成空文件 → 改为先序列化到内存 + 临时文件
  `os.replace`（`tests/test_history_events.py` 钉住：失败时磁盘上旧文件分毫不动）
- **提示词**：`_METHODOLOGY` 输出格式加可选字段「图表」（给前端挑默认图型）；
  `loop._EXCLUDED_FROM_CONFIRMED` 把它排除出沉淀回注（避免挤占 240 字符预算）

**验收**：`pytest -o addopts="" -q` = **683 passed**（基线 658 + 新增 25）。视觉规范先出原型页
（Altair 生成 spec + vega-embed 渲染）经用户确认后才落地。

**独立审核（2026-09-20）**：审核 agent 只读证伪，7 条声明 6 条**已证实**、1 条**部分证伪**
（`chart_hint` 只传当轮、不传回放 → 同一条消息刷新后默认图型会变），另挖出 8 条未覆盖风险。
逐字搬运一条经 AST 穷举确认：16 个函数里 15 个逐字等价（第 16 个就是本次有意重写的调度入口）；
色值搬运零偏差。已修：

- `chart_hint` 回放补齐（并加 AppTest 钉住：有提示走提示、无提示回落到数据形态）
- **`level_orders` 随消息落盘**：回放复现**当时**的层级序与树 —— 此前用「此刻」的语义层，
  层级序一改历史树就静默改观（审核实证同一消息两种画法会多出一条边）
- 瀑布图补走 `themed()`：搬运时保持原样，导致它是同页**唯一**没统一字体/网格/轴色的图
- `.tmp` 名改为唯一 + 失败时清理：原名写死，两个浏览器标签页并发写会交叠损坏共享文件
  （审核已构造复现），且失败会留下含聊天记录明文的残骸
- `plan_tasks` 的 `limit=0` 负零切片陷阱（`items[-0:]` 会整批放行）

未修、记录在案的两条见 §4。

**未做**：多指标查询（散点图 / 双轴组合图的前提）—— 属独立决策，本期明确不做。

**改这块代码前必读**：

- `app/` 内模块的导入已统一为**平铺名**（`from chart_data import ...`），`app/` 的测试靠
  `sys.path.insert(APP_DIR)`（与 `tests/test_import_wizard.py` 同模式）
- `attribution_viz.py` 对新模块用**函数内延迟 import** —— 模块级 import 会与 `chart_attr` 成环
- 配色与 mark 级样式走 `chart_theme.themed()` 的 `configure_*` 层，**不要**写进 `mark_bar(...)`：
  有测试精确断言 `spec["mark"] == {"type": "bar"}`

---

## 4. 已知问题（不阻塞，但别忘）

| 问题 | 影响 |
|---|---|
| `app/app.py` **658 行**（>300） | HEAD 起的历史欠账；新逻辑已尽量外移（M3-b 的战场，届时处理） |
| `scripts/generate_data.py` **359 行**（>300） | 同上 |
| **`datasets/ecommerce-demo/semantic.yaml` 缺 `caveats` 节点** | 提示词里的「口径陷阱」段永远为空。渲染路径有测试覆盖，**加上节点即自动生效** |
| `scripts/check_semantic.py` 的**影响分析只做了一半** | ✅ 已修(2026-09-14):破坏性变更时列出受影响 case(`datasets/<id>/cases/*.yaml` 的 stem)与 annotations.jsonl 是否存在;语义层不在数据集包布局里时降级为提示 |
| `attribution/decompose.py` / `engine_decompose.py` / `semantic_schema.py` **恰好 300 行** | 零余量，下一个改动即破线（M3-a 已把 semantic 系列从超限压回线内，别再往上加） |
| **`CHART_LIMIT` 是每条消息的上限，不是页面总量**（审核 2026-09-20） | 会话内消息数无上限（`MAX_SESSIONS=30` 只限会话数）。极端情形：40 条 assistant 消息 × 上限 24 = 每轮 rerun 最多 960 张图；`_save_history` 每次追加都全量重序列化（实测 40 条 ≈ 2.5 MB / 17 ms，当前量级可接受，但**没有防护栏**） |
| **结构性 `total_*` 与 `query_metric` 同页并置**（审核 2026-09-20） | 新增的查询图让两类**不可比**的总量会同页出现。各自独立成图，没破「不得画进同一张瀑布图 / 同一条叙事线」的红线，但读者更容易并排比较 —— 低严重度，仅提示 |
| **行数要用 `ReadAllLines` 量**（2026-09-20 实测） | `Get-Content $f \| Measure-Object -Line` 读无 BOM 的中文文件会**系统性少算 20–45 行**（实测 `attribution_viz.py` 303 vs 282、`app.py` 253 vs 214、`tool_catalog.py` 158 vs 122）。按少算的值估余量会误判 —— 本文件上面那条「303 行」的记录其实是**对的**，是测量方法错 |
| 整数维度的返回值类型变过 | `query_metric('gmv',['year'],…)` 从 `2026.0` 变成 `2026`（新代码保持列原始类型，**属有意改进但未正式记录**） |
| **单侧切片用例依赖开店日期分布** | `tests/test_engine_decompose.py` 的单侧切片用例依赖 2024-09 开店分布；重新造数若改开店策略会因数据变红 |
| **δ 近似路径不满足零残差**（审核证伪） | 因子含非正值时 Σeffect ≠ total_change（近似,label 有标注）。已写入契约 v2 警告;不要宣称「任何情况下零残差」 |
| **structural 的 `total_*` 与 `query_metric` 不可比**（审核 R1） | 总量定义在保留实体上（分母不可加 + 丢弃零分母实体）,与指标标量数值不同、可方向相反。契约 v2 已警告;**M3-b 画图时不得把两者混画** |
| **structural 会丢弃根因实体**（审核 R2） | ✅ 已修(2026-09-15):`_structural_split` 不再丢弃任一侧分母为 0 的实体,改为单列进 `entity_changes`(整窗与切片都带,键 = {entity,label,only_in,numerator_base,numerator_cmp,denominator_base,denominator_cmp}),不并入 effects——零残差恒等式不受影响。下架 SKU `SKU_P0001` 在 `entity_changes` 可见,`only_in == "base"` |
| **`contribute` 输出不可复现**（审核 R3） | ✅ 已修(2026-09-14):`_top_additive`/`_top_ratio` 先按 key 定序、再按 change/change_rate 升序(None 放后),同输入→同输出 |
| **`contribute` 空窗口抛 TypeError**（审核 R4） | ✅ 已修(2026-09-14):任一侧整窗无数据抛 DecomposeError,与 decompose 统一 |
| **防回退扫描器有结构性盲区**（审核 R6） | 词汇表只收元数据(指标名/列名/维度键),**实体取值**(STORE_S0001、SKU_P0001、门店名等)在结构上扫不到。M4 或专项把实体取值补进 conftest |
| **提交信息误导**（审核 R7） | `attribution/anomaly.py`/`decompose.py` 实际随「M1–M2 完成」提交(75a2ac9)提交,追溯时别被信息误导 |
| **structural 的 factors 是装饰性的**（审核 R8） | 不校验存在性、实现也不消费(只用 entity_dimension + depends_on)。语义层注释已改为与实现一致的口径;若 M5 数据集的声明语义更强,需再议 |
| **contribute 工具没有 filters 参数**（M3-b 发现） | ✅ 已修(2026-09-14):引擎与工具均支持 filters(与 query_metric 同口径,同时作用于整窗总量与切片),契约 v2.2 已固化 |
| **decompose 未接线为工具** | ✅ 已修(2026-09-14):`decompose` 成为第 5 个模型可见工具(schema 渲染层拆至 `harness/tool_catalog.py`,target 枚举来自分解声明、因子由工具代填),系统提示词方法论已补「机制分解」步骤 |
| **瀑布图 stack="zero" 负累计缺陷**（M3-b 自报） | ✅ 已修(2026-09-15):瀑布图区间 y0/y1 改为 `_waterfall_rows` 在 Python 里显式计算,不再交给 Vega 的 stack——累计跌破 0 也不会拆栈(回归测试 `test_render_skips_empty_and_emits_explicit_interval_chart` 钉住) |
| **「其余切片」标签三种情形共用**（M3-b 自报） | ✅ 已修:截断 / 空 top / 不可读三种标签分开(修订轮)。注意截断标签写的是「已省略 N 条之外的切片」,N 是**已进图**的条数(contribute 不回传总数,签名冻结) |
| **未知形态结果静默不画**（M3-b 修订残留） | ✅ 已修(2026-09-15):`render_from_events` 增加兜底 else 分支——既无 totals 又无 change_rate 也无 error 的 dict,如实给一行「结果形态无法识别,不画瀑布图」caption(回归测试 `test_render_from_events_hints_on_unknown_result_shape`) |
| **app.py 658 行**（>300,历史欠账） | ✅ 已修(2026-09-15):拆至 243 行,逻辑外移至 `app/attribution_viz.py`(可视化)+ `app/conclusion.py`(结论解析)+ `app/format.py`(展示辅助)+ `app/history.py`(会话历史)+ `app/theme.py`(全局样式)。行为不变、公共名可 import |
| **评估真跑路径已验证**（2026-09-14 修复+M4 遗留闭环） | 首跑暴露「语义层未初始化」:`evaluate()` 绕开 CLI/前端直接进 loop,全局引擎单例未注入。已补 `tools.init_engine(data_dir, semantic_path)`(延迟 import)。live 首跑见 §2「模型双协议接入 + 评估 live 首跑」 |
| **mock 剧本会制造虚假自信**（M4 自报） | mock 准确率是剧本写死的,且 mock 结论从 case.expected 生成——「定位命中」维只在剧本配合范围内走过。引用时不许说「agent 能答对一半」 |
| **c3 可能误伤啰嗦的诚实答案** | must_not_claim 用完整短语匹配已缓解;但机械评分 vs 语义正确的张力在 L2+ case 上会一直存在,报准确率时注明 |
| **c1/c2 的 contribution_range 偏宽** | contribute 已加 filters(2026-09-14);评分口径是否收窄、case 是否改用过滤下钻,留给评估侧修复轮 |
| **HANDOFF R3/R4 仍未修**（M4 自报） | ✅ 已修(2026-09-14):R3 并列定序兜底、R4 空窗口 DecomposeError(见上两条)。两轮 live 成绩 3/9→4/9 不可复现属 2026-09-14 历史事实,引用准确率仍须注明批次 |
| **深度维静默降级**（M4 自报） | ✅ 已修(修订轮):BI_DATASET 传包根目录 + 解析失败抛错。**live 模式接线时必须继续传包根目录**(见 evaluate 里的注释),评分上下文靠环境变量是单线程假设 |
| **`--sample` 过滤未实现**（M4 修订裁决） | §7③ 的四个过滤器做了三个;随机抽样延后(引入不确定性需单独决策) |
| **mock 覆盖绑定出厂 case id** | `SCRIPTS_BY_ID` 硬编码 c1-c4;case 改名后静默退回 crc32 轮换,「每维有失败样本」的保证只有测试钉住 |
| **根因 case 上自相矛盾的答案不扣分** | ✅ 已修(2026-09-14):有期望根因的 case 宣布「无法归因」却给出 key,定位/深度两维都不作数(见 `eval_report.score_case` 互斥分支) |
| **`expectations: []` 显式声明仍可 0/0 全绿** | F7 有意保留的口子(作者显式声明=意图)。若 CI 用回验做门禁,需另行要求非空 |

---

## 5. 关键约定与陷阱（这次会话反复踩的）

### 5.1 机械防回退：**不要绕过它**

`tests/test_engine_has_no_hardcoded_vocabulary.py` 用 AST 扫描 `attribution/` 与 `harness/`
的所有模块（**默认全扫，例外清单只排除 `attribution/self_test.py`**——它是数据集专属验收脚本，
合法地含大量词汇），断言**不出现任何数据集词汇**（指标名/维度名/表名/列名/`decompositions.factors`/日历）。

- **docstring 与注释里的举例是允许的**（扫描器剔除 docstring，注释不进 AST）
- **可执行代码里的字符串字面量不行**
- 词汇表由 `tests/conftest.py` 从语义层 YAML + 真实 parquet 的列名推出（**41 词**）
- **新增模块自动被覆盖**——但如果你往 `app/` 写代码，注意它**不在扫描范围**

### 5.2 破坏性变更要走 `check_semantic.py`

改 `expression`（口径）/ `time_aggregation` / `hierarchy` 顺序 / 删指标维度 = **破坏性**。
提交前跑：

```powershell
python scripts/check_semantic.py --old <旧> --new <新>
```

退出码非 0 时必须：① bump `dataset_version` ② 重算受影响 case。

### 5.3 语义层是地图，不是配置

- **场景内固定，场景间可变**。改地图要留痕（`dataset_version`），因为**评估结果绑定它**
- 前端**只读**它，不做编辑——编辑会绕过整套治理

### 5.4 「不可比」要如实说，不要编数

这是本项目反复出现的判据：
- 异常检测：数据不足 → `baseline_type="none"` + `base=None`，**不用 0 或邻窗冒充**
- 语义层 diff：`Semantic` 实例看不到非结构化块时标为**不可比**，而不是"被删除"
- 归因 agent：找不到真因时应当报「无法归因」

### 5.5 验证脚本本身会骗人

本次会话**两次**被我方验证脚本误导：
- 一个比对脚本因正则漏写 `DOTALL`，多行 JSON 根本没被归一化
- 一个"行为不变性"脚本只覆盖 15 个 `query_metric` 切片，**结构上不可能**发现整数类型变化，
  且 `contribute` / `detect_anomaly` 的输出**从未被比对过**

**结论：验证要覆盖边界与错误路径，不能只跑 happy path。**

---

## 6. 未决事项

1. **异常检测实现深度**——已按「自写同星期几中位数 + MAD」实现（无新依赖），
   是否还要引入 `statsmodels` 做 STL，未定
2. **M5 顺序**——建议 `saas-mrr` 先行（验证设计），但若更看重「营销」这个对外定位，
   也可以 `marketing-funnel` 先行
3. **30-case 准确率报告是否对外**——要不要放进 README / 作品集展示
4. **整数类型变化**是否正式记录为「有意改进」（见 §4 最后一行）
5. ~~git 提交粒度~~ ✅ 已定：M3–M6 的改动随本会话一次整体提交（跨里程碑的文件改动已交错，无法干净拆分）

---

## 7. 工作方式（本项目已跑通的模式）

**每个里程碑：实现 → 独立审核闸门。**

实现阶段用多个并行 agent，**但必须保证任意两个 agent 不共享可写文件**；
先由主 agent 冻结接口（写骨架文件：签名 + docstring + `NotImplementedError`），再并行实现。

审核 agent 的任务是**证伪**，不是复核：
- 给它「待证伪的声明清单」+ 重点怀疑对象
- 要求它**自己设计检查方法**，不能只重跑别人给的脚本
- **必须做受控实验**（插入→验证→还原→**用哈希证明已还原**）
- 结论区分「已证实 / 已证伪 / 无法判定」
- 最后单列「未被声明覆盖的风险」

**M1 的审核就是靠这套推翻了主 agent 的结论**，挖出一个结构上不可能被原验证脚本发现的偏差。

---

## 8. Subagent 分工

§7 讲的是**方法论**（怎么派活），本节是**实录**（实际怎么派的）与**剩余工作的分工建议**。
恢复工作时照 §8.2 直接派活即可。

### 8.1 已执行的分工实录

每个阶段都是「主 agent 冻结接口 → 并行实现 → 主 agent 集成 → 独立审核」。

**M1 语义层可执行化**

| 阶段 | 执行者 | 归属文件 |
|---|---|---|
| P0 冻结接口 | 主 agent | `attribution/expression.py`(骨架)、`attribution/semantic.py`(骨架)、`pytest.ini` |
| P1 并行 | Agent A | `attribution/expression.py` 实现 + `tests/test_expression.py`（36 用例） |
| P1 并行 | Agent B | `attribution/semantic.py` 实现 + `tests/test_semantic_schema.py`（30 用例） |
| P1 并行 | Agent C | `tests/conftest.py`、`tests/test_engine_has_no_hardcoded_vocabulary.py`、`semantic/semantic.yaml` 升 v2 |
| P2 集成 | 主 agent | `attribution/engine.py` 改造（去硬编码）、语义层补除零守卫 |
| P3 审核 | 审核 agent | 只读。**推翻了主 agent 的「行为不变性」结论**，挖出整数类型变化 |

**M1 收尾重构**（拆分超限文件、清理边界）

| 执行者 | 归属文件 | 结果 |
|---|---|---|
| Agent D | `attribution/semantic.py` → 拆出 `attribution/semantic_schema.py` | 504 → 292 + 274 |
| Agent E | `attribution/engine.py` → 抽数据访问层 `attribution/sql_source.py` | 318 → 210 + 208 |
| Agent F | `attribution/semantic.py` + `semantic_schema.py` 边界重切 | 17 个私有名反向 import → 0，mixin 消失 |
| Agent G | `tests/conftest.py`、`tests/test_engine_has_no_hardcoded_vocabulary.py`、拆 `test_semantic_schema.py` | 词汇表 28 → 41，扫描改为「默认全扫 + 例外排除」 |

> D/E/F 是**串行**的（都动 `semantic.py` 或 `engine.py`）；G 与它们并行（只动 `tests/`）。

**M2 数据集包化与动态化**

| 阶段 | 执行者 | 归属文件 |
|---|---|---|
| P0 冻结接口 | 主 agent | `harness/datasets.py`(骨架) |
| P1 并行 | Agent A | `harness/datasets.py` 实现 + `datasets/ecommerce-demo/` 迁移 + `harness/run.py` + `attribution/self_test.py` 路径 + `tests/test_datasets.py`（31 用例） |
| P1 并行 | Agent B | `harness/tools.py`、`harness/context.py`(新)、`harness/loop.py`、`tests/test_tools_schema.py`（14 用例） |
| P1 并行 | Agent C | `attribution/semantic_diff.py`、`attribution/semantic_diff_rules.py`、`scripts/check_semantic.py` + 2 个测试文件（42 用例） |
| P2 集成 | 主 agent | 修 `tests/conftest.py` 的 `DATA_DIR`（见下方教训） |
| P2 并行 | Agent D | `app/dataset_selector.py`(新)、`app/app.py`、`app/semantic_view.py` |
| P3 审核 | 审核 agent | **已启动但被叫停，未完成** → 若要补审，重点见 §8.3 |

**M3 分析能力深化 ✅（2026-09-13 收尾）**

| 阶段 | 执行者 | 归属文件 | 状态 |
|---|---|---|---|
| P0 冻结接口 | 主 agent | `attribution/decompose.py`(骨架) | ✅ |
| P1 并行 | Agent A | `attribution/decompose.py` + `tests/test_decompose.py` | ✅ 完成（111 用例；测试文件已拆为 6 个 ≤300 行文件） |
| P1 并行 | Agent B | `attribution/anomaly.py` + `tests/test_anomaly.py`（21 用例） | ✅ 完成 |
| P2 接线 | 实现 agent | `attribution/engine.py` + `sql_source.py` + `engine_decompose.py`(新) + `semantic_decompose.py`(主 agent 收尾拆分) + 26 项新测试 | ✅ 完成 |
| P2 裁决 | 主 agent | **B 案例 lookback 裁决**：618 干扰项验收在 1× 回看下不成立（见 §2），改为 **2× 回看**；绊线测试 `tests/test_engine_anomaly.py` | ✅ 完成 |
| P3 审核 | 审核 agent | 只读 | ✅ 完成（2026-09-13）：10 条声明 9 证实 1 证伪；挖出 9 条未覆盖风险，处置见 §4 |

### 8.2 剩余工作的分工建议

| 阶段 | 建议并行度 | 归属文件 | 审核重点 |
|---|---|---|---|
| ~~M3-a 接线~~ | ✅ 已完成 | （见 §8.1 M3 实录） | —— |
| **M3-b 可视化** | 1 个 | `app/`（**新逻辑必须放独立模块**，`app.py` 已 658 行） | 图能渲染；不加新依赖 |
| **M4 评估脚手架** | 2 个并行 | ① `harness/loop.py`（结构化结论）+ 其测试<br>② `scripts/evaluate_agent.py` + case schema + `datasets/*/cases/` | 评分是否真机械可判；准确率的分母有没有被偷偷缩小 |
| **M5 数据集** | **建议串行** | 每个数据集 1 个 agent：`datasets/<name>/` 全包（造数 + 语义层 + case + `ground_truth.yaml`） | 埋点能否被**回验**；切片×时间是否真隔离；半可加是否被拦住 |
| **M6 语义收集** | 2 个并行 | ① `attribution/profile.py` + `scripts/profile_data.py`<br>② `attribution/annotations.py` + `harness/context.py` 注入 | profiler 草稿与人工语义层比对；注入是否选择性 |

**M5 为什么建议串行**：四套数据集彼此独立，理论上可全并行；但 `saas-mrr` 是用来**验证 schema v2 的新表达能力
（半可加 / 跨粒度 / 同期群）设计得对不对**的——如果它推翻设计，另外三套就得返工。所以先做最小的一套。

### 8.3 文件归属速查（派活前先看这张表）

| 文件/目录 | 谁改过 | 备注 |
|---|---|---|
| `attribution/engine.py` | M3-a 实现 agent | detect_anomaly 日历感知 + decompose 入口（276 行） |
| `attribution/sql_source.py` | M1 Agent E + M3-a | 新增 `daily_series`（229 行） |
| `attribution/engine_decompose.py` | M3-a 实现 agent | **恰好 300 行，零余量**——decompose 编排主体 |
| `attribution/semantic_decompose.py` | 主 agent（M3-a 收尾） | 分解声明/日历的定义、解析与纯规则（167 行） |
| `attribution/decompose.py` | M3 Agent A | **恰好 300 行，零余量**（纯函数，勿动） |
| `attribution/anomaly.py` | M3 Agent B | 已完成（295 行），引擎只消费 |
| `attribution/semantic*.py` | M1 Agent D/F + 主 agent | 边界已重切干净，不要倒退；semantic_schema 又到 300 行线 |
| `attribution/*_diff*.py` | M2 Agent C | |
| `harness/tools.py` · `context.py` · `loop.py` | M2 Agent B | M4 要动 `loop.py`（结构化结论） |
| `harness/datasets.py` · `run.py` | M2 Agent A | |
| `app/app.py` · `dataset_selector.py` · `semantic_view.py` | M2 Agent D | **M3-b 的战场** |
| `tests/conftest.py` | M1 Agent C → M2 主 agent | **共享度高，最容易被冻结成阻塞点**（见下） |
| `docs/REUSE_DESIGN.md` · `CLAUDE.md` | **一律主 agent** | 派活时禁止 subagent 改 |

### 8.4 两条从实录里得来的教训

**① 不要把「所有人都可能碰」的文件冻结给某个 agent 的禁改清单。**
M2 时主 agent 把 `tests/conftest.py` 列入 A 的禁改清单（怕与并行的防回退 agent 撞车），
结果那个 agent 先退场了，而需要改它的 A 又被禁着——**12 个测试红了却没人能修**，
最后由主 agent 手工修好。**共享文件的禁改要带时限**，或干脆由主 agent 自己持有。

**② 主 agent 必须自己留一部分（集成 + 验收），不要把全部工作派出去。**
每轮的「P2 集成」都是主 agent 做的，而且**总能发现 agent 之间对不齐的地方**
（如 M1 的 `_scalar` 列表归一化、M3 的骨架与实现的接口缝隙）。
