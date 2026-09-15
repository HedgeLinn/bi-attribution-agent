# 归因引擎接口契约

> 这是归因引擎与 harness 主线之间的合同。字段名、函数签名都是契约,双方都不得改。
> 最后更新:2026-09-14(v2:新增 decompose、detect_anomaly 改为日历感知;
> contribute 增 filters 参数、空窗口统一 DecomposeError、top 排序 key 定序兜底)

## 依赖

- 语义层:`datasets/<name>/semantic.yaml`(schema v2,字段名不得改;M2 起语义层随数据集打包)
- 数据:`datasets/<name>/data/*.parquet`(DuckDB 直接读)

## 模块结构

```
attribution/
  __init__.py
  engine.py              # 唯一对外暴露 AttributionEngine 类
  engine_decompose.py    # decompose 的编排实现(engine.py 有单文件行数约束)
  sql_source.py          # 数据访问层(Repository,不对外)
  decompose.py           # LMDI 纯函数(四种分解,零残差)
  anomaly.py             # 日历感知异常判定(纯函数)
  semantic*.py           # 语义层定义与校验
```

harness 只 `from attribution.engine import AttributionEngine`,不关心 engine 内部实现。

## 类与签名

```python
class AttributionEngine:
    def __init__(self, data_dir: str, semantic_path: str): ...
    def query_metric(self, metric, dims, filters, start, end) -> dict: ...
    def contribute(self, metric, dimension, level, base_start, base_end, cmp_start, cmp_end, top_k=5) -> dict: ...
    def detect_anomaly(self, metric, start, end, cmp_start, cmp_end, threshold=0.15) -> dict: ...
    def decompose(self, target, factors, base_start, base_end, cmp_start, cmp_end,
                  dimension=None, level=None, filters=None, top_k=5) -> dict: ...   # v2 新增
```

## 方法 1:query_metric

查询某个指标在时间范围内的聚合值,可按维度字段分组、可按过滤器限定切片。

```python
def query_metric(
    self,
    metric: str,            # 语义层 metrics 里声明的指标名
    dims: list[str],        # 维度字段名列表,如 ['region'] 或 ['region','city'] 或 [] (不分组)
    filters: dict,          # {'store_id': 'STORE_S0001', 'category': '手机'} 或 {}
    start: str,             # 'YYYY-MM-DD'
    end: str,               # 'YYYY-MM-DD'
) -> dict
```

返回结构:
```python
{
    "metric": "gmv",
    "total": 1234567.0,           # 全量聚合值(dims 为空时才有意义,否则为 None)
    "rows": [                      # dims 为空时 rows 只含一行聚合结果
        {"region": "华东", "value": 88888.0},
        ...
    ]
}
```
- `dims` 里的字段,必须是某个维度 hierarchy 里的合法字段。
- 过滤字段同理,来自维度 hierarchy 字段或维度 key。

## 方法 2:contribute(核心:贡献度下钻)

对 additive 指标做「变化量按切片分解」,对 derived 指标退化为「变化率排序」。

```python
def contribute(
    self,
    metric: str,
    dimension: str,          # 语义层维度名:'store' | 'product' | 'channel' | 'date'
    level: str,              # 该维度 hierarchy 里的某一层,如 'region'/'city'/'store_id'
    base_start: str, base_end: str,   # 基准期
    cmp_start: str, cmp_end: str,     # 对比期
    top_k: int = 5,
    filters: dict | None = None,      # v2.2 新增:过滤切片域,与 query_metric 同口径
) -> dict
```

边界行为(v2.2 固化,此前为缺陷):

- `filters` 同时作用于整窗总量与切片两侧(过滤字段用 ID,落在维度表的键自动 join)。
  不带 filters 时各总量与切片口径同旧版,逐值不变。
- 任一侧整窗无数据(总量为 None)抛 `DecomposeError`——「没有行不等于 0」;
  此前是 `None - None` 的 TypeError,现与 decompose 统一错误类型。
- `top` 排序**先按 key 定序、再按 change / change_rate 升序**(None 放后):
  并列时输出稳定,同输入 -> 同输出,不随 DuckDB 行序噪声漂移。

### additive 指标
```python
{
    "metric": "gmv",
    "dimension": "store",
    "level": "region",
    "total_base": 13000000.0,
    "total_cmp": 10800000.0,
    "total_change": -2200000.0,     # = total_cmp - total_base
    "top": [                          # 按 change 升序(最拖累的在前)
        {
            "key": "STORE_S0001",     # 该切片的原始取值(level==key 时是主键,中间层是字段值本身)
            "label": "华东",          # 该层切片的显示名(level==key 时用 name 列,否则用字段值本身)
            "base": 3500000.0,
            "cmp": 2900000.0,
            "change": -600000.0,
            "contribution": 0.2727,   # = change / total_change (total_change==0 时置 None)
        },
        ...
    ]
}
```

### derived 指标
无法加法分解,`contribution` 一律置 None,`change` 改为 `change_rate`:
```python
{
    "metric": "aov",
    "dimension": "store",
    "level": "region",
    "total_base": 589.0,
    "total_cmp": 553.0,
    "total_change": -36.0,
    "top": [
        {"key": "华东", "label": "华东", "base": 526.0, "cmp": 528.0, "change_rate": 0.0038},  # (cmp-base)/base
        ...
    ]   # 按 change_rate 升序
}
```

## 方法 3:detect_anomaly(日历感知,v2 行为变更)

```python
def detect_anomaly(self, metric, start, end, cmp_start, cmp_end, threshold=0.15) -> dict
```

**v2 起**判定由 `attribution/anomaly.py` 完成(§4.4):
- 取该指标在 `[start − 56 天, end]` 的**日序列**,基线由窗口之前的「近 4 周同星期几中位数」估计(促销日历内的样本剔除),对比值是窗口内日值的中位数。序列回看取估计窗口的 **2 倍**——日历剔除最多可吃掉一整个估计窗口的样本,1× 时干净基线会短到连同星期几匹配都做不成(实测 618 场景把自然回落误报成异常)。
- 窗口命中语义层 `time.calendar` 的日历条目时 `is_expected=True`(属预期脉冲)。
- **`cmp_start` / `cmp_end` 不再使用**(基线不再由调用方指定基期,改由历史估计);参数保留仅为签名兼容。
- 历史不足时如实退化(`baseline_type`: `weekday_matched` / `flat` / `none`),**不编基线**。

返回(v2 只增不改,旧键保留):
```python
{
    "metric": "gmv",
    "base": 414966.0,          # 稳健基线(同星期几中位数);数据不足时为 None
    "cmp": 346822.0,           # 对比窗口日值中位数;窗口无数据时为 None
    "change": -68144.0,
    "change_rate": -0.1642,    # (cmp-base)/base,四舍五入 4 位;基线为 0 时为 None
    "is_anomaly": True,        # abs(change_rate) >= threshold
    "baseline_type": "weekday_matched",   # v2 新增:weekday_matched | flat | none
    "is_expected": True,       # v2 新增:窗口命中促销日历
    "anomaly_kind": "pulse",   # v2 新增:pulse | level_shift | trend_change | None
    "note": "基线取近 4 周同星期几的中位数(已消除周内效应);...",   # v2 新增:中文判定说明
    "baseline_mad": 123.0,     # v2 新增:基线 MAD
    "robust_z": -2.5,          # v2 新增:相对 MAD 的偏离倍数
}
```

## 方法 4:decompose(LMDI 分解,v2 新增)

把 target 在基期→对比期的总变化拆成因子的效应,**零残差**(Σeffect ≡ total_change)。

```python
def decompose(self, target, factors, base_start, base_end, cmp_start, cmp_end,
              dimension=None, level=None, filters=None, top_k=5) -> dict
```

**前置**:`(target, factors)` 必须命中语义层 `decompositions` 的一条声明(因子集合一致,顺序任意);
kind 与 entity_dimension 以声明为准,未命中抛 SemanticError。分解必须走「地图」,不许现编。

**四种 kind**(声明里的 `kind`):
| kind | 恒等式 | 因子取值 |
|---|---|---|
| `multiplicative` | V = Π factors | 因子 = 该指标在窗口内的标量聚合 |
| `additive` | V = Σ factors | 同上(允许负值) |
| `ratio` | V = 分子 / 分母 | factors 恰两个:分子在前、分母在后 |
| `structural` | V = Σ (实体权重 × 实体强度) | 权重 = 分母的实体占比,强度 = 分子/分母(实体级);target 必须是「分子/分母」形 derived 指标,实体来自声明的 `entity_dimension` |

**维度形态**:不带 `dimension`/`level` 时对全窗分解一次;带时对每个切片各分解一次
(`level` 必须在 `dimension` 的 hierarchy 里),切片按 `|total_change|` 降序取前 `top_k`。

**边界**:因子含非正值走 δ 替代并标注「近似」;`total_change == 0` 时 contribution 为 None;
切片只在单侧窗口出现时,缺失一侧按 0 处理;`filters` 同时作用于 target 与全部因子。

**两个必须知道的警告**(M3-a 独立审核实测,勿踩):

1. **δ 近似路径不满足零残差。** 零残差(`Σeffect ≡ total_change`)只在因子全部为正时成立;
   任一因子为非正值时走 δ 替代,该路径的效应之和**不等于** `total_change`(为近似值),
   对应 effect 的 label 带「(含非正值,效应为近似)」标注。消费方看到该标注时不得把
   效应当精确量使用。
2. **structural 的 `total_*` 不是指标自身标量,不可与 `query_metric` 混用。**
   结构分解的总量定义在「保留实体」上(`Σ分子 / Σ分母`,分母为 0 的实体两侧一并丢弃),
   当分母不可加(如 `COUNT(DISTINCT)`)时,该总量与 `query_metric` 的窗口标量**数值不同、
   甚至方向相反**。下钻叙事时不要拿 `query_metric` 的值与结构分解的 `total_*` 直接对比。
   分母为 0 的实体不进入 LMDI 效应,但会**单列进 `entity_changes`**(见返回结构)——
   「下架/新上」类根因实体就记在那里,消费方看 `entity_changes` 而不是 effects。

返回:
```python
{
    "target": "gmv",
    "kind": "multiplicative",
    "factors": ["aov", "orders_count"],
    "dimension": None, "level": None,          # 或具体值
    "total_base": 13692893.0,
    "total_cmp": 10824508.0,
    "total_change": -2868385.0,
    "effects": [                                # 整窗分解,恒有
        {"factor": "aov", "label": "客单价", "base": 823.4, "cmp": 725.1,
         "effect": -1600000.0, "contribution": 0.558, "change_rate": -0.119},
        ...
    ],
    "entity_changes": [                        # 仅 structural:下架/新上实体(任一期分母为 0)
        {"entity": "SKU_P0001", "label": "数码家电手机通讯-001号",
         "only_in": "base", "numerator_base": ..., "numerator_cmp": ...,
         "denominator_base": ..., "denominator_cmp": ...},
        ...
    ],
    "slices": [                                 # 仅带 dimension/level 时出现
        {"key": "STORE_S0001", "label": "上海徐家汇旗舰店",
         "total_base": ..., "total_cmp": ..., "total_change": ...,
         "effects": [...], "entity_changes": [...]},   # structural 切片各带自己的 entity_changes
        ...
    ]
}
```

## 半可加指标(time_aggregation: last,v2.1 新增)

`type: semi_additive` 且 `time_aggregation: last` 的指标(如 MRR / DAU / 余额),查询语义
与可加指标完全不同——**窗口内取最后有数据日的值,而不是求和**(求和会得到 §1.3 的
静默错误,如「1 月 + 2 月 + 3 月 MRR」= 3 倍):

| 入口 | 半可加语义 |
|---|---|
| `query_metric` 整窗 | 窗口内最后有数据日的值;无数据返回 None(不是 0) |
| `query_metric` 分组 | 每个分组各自的末日值(组内按日期倒序取第 1 行) |
| `contribute` 切片 | 每切片每窗口的末日值(同上口径) |
| 日序列(detect_anomaly 的输入) | 每日值(日粒度天然可加,不变) |

其它 `time_aggregation`(avg / max)尚未实现查询语义,遇到会抛 SemanticError——
宁可失败,也不静默按 sum 算出一个错的数。半可加的时间聚合变化(sum ↔ last)属
破坏性语义层变更,走 `check_semantic.py` 的变更治理。

## 实现要点(不约束内部写法)

1. **SQL 生成**:additive 指标按 `expression` 里的 `SUM(...)`/`COUNT(DISTINCT ...)` 生成 DuckDB SQL;derived 指标先查其 `depends_on` 的底层指标再相除(同一粒度、同一过滤、同一时间窗下计算,避免粒度错位)。
2. **维度 join**:按某维度 level 聚合时,join 对应维度表,group by 该 level 字段。
3. **label 规则**:level 是 hierarchy 中间层时 label=字段值本身;level==key 时 label 取该维度表的 name 列。**每个 top/切片项额外返回 `key` 字段**,为切片的原始取值,供下游 query_metric 的 filters 直接引用。
4. **贡献度口径**:`contribution = change / total_change`。total_change==0 时置 None(防止除零)。
5. 全部方法返回**纯 dict**(可 JSON 序列化),不要返回 DataFrame 或 duckdb 内部对象。
6. **无硬编码**:引擎不认识任何具体数据集,指标/维度/因子名一律来自语义层与入参;有 AST 扫描测试机械保证(数据集词汇出现在 attribution/ 可执行代码里会挂测试)。

## 自测要求

`attribution/self_test.py` 用真实数据集跑一遍,验证:
- `query_metric` 各 region 有值
- `contribute('gmv', 'store', 'store_id', ...)` 的 top[0] label 为「上海徐家汇旗舰店」,change 显著为负
- `detect_anomaly('gmv', '2026-06-01','2026-06-30','2026-05-01','2026-05-31')` 的 is_anomaly 为 True

## 依赖契约总结(双方都要遵守)

- 语义层字段名与取值:见 `docs/REUSE_DESIGN.md` §3.2(schema v2)与各数据集语义层。
- 维度表 name 列、时间字段格式由语义层声明,引擎不假设任何具体名字。
