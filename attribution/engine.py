"""归因引擎 AttributionEngine。

契约:docs/ATTRIBUTION_CONTRACT.md;语义层经 attribution.semantic.Semantic 加载;
数据访问下沉到 attribution.sql_source.SqlSource(Repository)。
对外暴露:query_metric / contribute / detect_anomaly / decompose(见各方法 docstring)。

所有方法返回纯 dict(JSON 可序列化),不返回 DataFrame / duckdb 对象。
本模块不认识具体数据集也不认识 DuckDB:分层是「SqlSource 取数 -> 这里算贡献度/分解/
异常 -> 结果整形」,算法层只消费原生数值,不认识连接、表名与 SQL。
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from attribution.anomaly import BASELINE_WEEKS_DEFAULT, assess
from attribution.decompose import DecomposeError
from attribution.engine_decompose import decompose_for
from attribution.semantic import TYPE_ADDITIVE, TYPE_SEMI_ADDITIVE, Semantic
from attribution.sql_source import SqlSource, introspect_columns

# 总变化量小于该值时不计算占比(避免除零放大噪声);量纲由指标本身决定
_MIN_TOTAL_CHANGE = 1e-12

# 回看周数 -> 天数的换算(基线窗口的起点 = 对比窗口起点往前推这么多天)
_DAYS_PER_WEEK = 7
_STAMP_FORMAT = "%Y-%m-%d"   # 日期口径:调用方、语义层与数据层统一用它
# 序列回看 = 基线估计窗口 × 该系数:促销日历剔除最多可吃掉一整个估计窗口的样本,
# 只取 1× 时干净基线会短到连同星期几匹配都做不成(实测:618 场景退化为 flat 且基线虚高,
# 把大促后自然回落误报成异常);2× 保证剔除之后仍有等宽的干净历史可供估计。
_LOOKBACK_MARGIN = 2


def _rnd(v, nd=4):
    """转 float,None 或 NaN 保持 None;到 nd 位小数(扰动噪声对判断无影响)。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    if nd is None:
        return f
    return round(f, nd)


def _native(v):
    """将 numpy 标量(int64/float64/bool 等)转成 Python 原生类型,保证 JSON 可序列化。"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    try:
        return v.item()
    except (AttributeError, ValueError):
        return v


def _stable_label(one, other):
    """同一 key 的两行 label 不一致时取确定的一个:合并顺序依赖 FULL JOIN 行序(不可控),
    不能「先见到谁用谁」——显式比较保证同输入 -> 同输出。"""
    if one == other:
        return one
    if one is None:
        return other
    if other is None:
        return one
    return one if str(one) <= str(other) else other


class AttributionEngine:
    def __init__(self, data_dir: str, semantic_path: str):
        # 语义层是唯一事实来源:口径、层级、显示名列全部来自它(§3.2 / §3.6)
        self.semantic = Semantic.load(
            semantic_path, columns=introspect_columns(data_dir)
        )
        self.semantic.assert_valid()

        # 数据访问层:表名、时间字段、SQL 构建与执行都归它管
        self.source = SqlSource(data_dir, self.semantic)

    def _scalar(self, metric: str, filters: Mapping[str, Any] | Sequence[Any] | None,
                start: str, end: str):
        """整窗标量聚合(无分组);结果整形为 float / None。

        filters 允许 None / [](等价于无过滤),归一化后再交给数据访问层。
        """
        filters = dict(filters or {})
        return _rnd(self.source.scalar(metric, filters, start, end), nd=None)

    # ------------------------------------------------------------------
    # 方法 1:query_metric
    # ------------------------------------------------------------------
    def query_metric(self, metric, dims, filters, start, end) -> dict:
        self.semantic.metric(metric)   # 未知指标 -> SemanticError

        if dims:
            records = self.source.aggregate(metric, dims, filters, start, end)
            rows = []
            for record in records:
                item = {d: _native(record[d]) for d in dims}
                item["value"] = _rnd(record["v"], nd=None)
                rows.append(item)
            # DuckDB 分组聚合的行序不保证稳定(并行 hash 聚合),同一份数据两次运行可能不同。
            # 这里显式排序,保证「同输入 → 同输出」,否则评估基准会被行序噪声污染。
            rows.sort(key=lambda item: tuple(str(item[d]) for d in dims))
            return {"metric": metric, "total": None, "rows": rows}
        total = self._scalar(metric, filters, start, end)
        return {"metric": metric, "total": total, "rows": [{"value": total}]}

    # ------------------------------------------------------------------
    # 方法 2:contribute(贡献度下钻)
    # ------------------------------------------------------------------
    def contribute(self, metric, dimension, level, base_start, base_end,
                   cmp_start, cmp_end, top_k=5, filters=None) -> dict:
        """贡献度下钻:filters 与 query_metric 同口径(过滤字段用 ID),
        同时作用于整窗总量与切片两侧——「只看某客群的区域下钻」才可表达。

        边界:任一侧整窗无数据抛 DecomposeError(没有行不等于 0,与 decompose 统一);
        top 排序先按 key 定序再按 change/change_rate,并列时输出稳定。
        """
        metric_def = self.semantic.metric(metric)
        dim_def = self.semantic.dimension(dimension)
        if level not in dim_def.hierarchy:
            raise ValueError(f"维度 {dimension} 无层级字段 {level}")

        mtype = metric_def.type
        filters = dict(filters or {})

        # 总期整窗标量(base / cmp)
        total_base = self._scalar(metric, filters, base_start, base_end)
        total_cmp = self._scalar(metric, filters, cmp_start, cmp_end)
        # 任一侧整窗无数据即无法算贡献度:与 decompose 同为「没有行不等于 0」口径,
        # 统一抛 DecomposeError(此前 None - None 抛 TypeError,对 agent loop 不友好)
        if total_base is None or total_cmp is None:
            raise DecomposeError(
                f"指标 {metric} 在其中一个窗口内没有任何数据:没有行不等于 0,无法计算贡献度")

        # 每切片:base 与 cmp 在同一 level 分组下各自聚合,再 FULL JOIN 配对
        rows = self.source.slice_rows(metric, dimension, level,
                                      (base_start, base_end), (cmp_start, cmp_end),
                                      filters)
        slices = self._slice_frame(rows)  # [(label, key, base, cmp)]
        total_change = _rnd(total_cmp - _rnd(total_base), nd=None)

        if mtype in (TYPE_ADDITIVE, TYPE_SEMI_ADDITIVE):
            # 半可加切片取的是同一基准日的水平值,与可加指标同样可做变化量分解
            top = self._top_additive(slices, total_change)
        else:  # derived / ratio
            top = self._top_ratio(slices)

        return {
            "metric": metric,
            "dimension": dimension,
            "level": level,
            "total_base": _rnd(total_base, nd=None),
            "total_cmp": _rnd(total_cmp, nd=None),
            "total_change": total_change,
            "top": top[:top_k],
        }

    def _top_additive(self, slices: Sequence[tuple], total_change) -> list[dict]:
        """additive:contribution = 切片变化 / 总变化;按 change 升序,None 放后。"""
        top = [
            {
                "key": key,
                "label": lb,
                "base": _rnd(bs, nd=None),
                "cmp": _rnd(cs, nd=None),
                "change": _rnd(cs - bs, nd=None),
                "contribution": (
                    _rnd((cs - bs) / total_change)
                    if total_change and abs(total_change) > _MIN_TOTAL_CHANGE else None
                ),
            }
            for lb, key, bs, cs in slices
        ]
        # 先按 key 定序再按 change 排序:并列时输出稳定(DuckDB 行序噪声不再影响 top 尾部,
        # 评估基准可复现——与 decompose 切片排序同一口径)
        top.sort(key=lambda x: str(x["key"]))
        top.sort(key=lambda x: (x["change"] is None, x["change"]))
        return top

    def _top_ratio(self, slices: Sequence[tuple]) -> list[dict]:
        """derived / ratio:contribution 一律 None;按 change_rate 升序,None 放后。"""
        top = []
        for lb, key, bs, cs in slices:
            bf, cf = _rnd(bs, nd=None), _rnd(cs, nd=None)
            change_rate = None
            if bf:
                change_rate = _rnd((cf - bf) / bf)
            top.append({
                "key": key,
                "label": lb,
                "base": bf,
                "cmp": cf,
                "change_rate": change_rate,
                "contribution": None,
            })
        top.sort(key=lambda x: str(x["key"]))
        top.sort(key=lambda x: (x["change_rate"] is None, x["change_rate"]))
        return top

    def _slice_frame(self, rows: Sequence[tuple]) -> list[tuple]:
        """FULL JOIN 结果按 key 合并(label 同 key 一起作为分组依据)。返回 [(label, key, base, cmp)]。"""
        merged = {}
        for label, key, base, cmp_ in rows:
            # 出现重复(key 相同但 FULL JOIN 按 lb join 可能重复)时累加,保证 slice 总计可对上 total
            if key in merged:
                old_label, _, pb, pc = merged[key]
                merged[key] = (
                    _stable_label(old_label, label),
                    key,
                    (pb if pb is not None else 0.0) + (base or 0.0),
                    (pc if pc is not None else 0.0) + (cmp_ or 0.0),
                )
            else:
                merged[key] = (
                    label,
                    key,
                    float(base) if base is not None else 0.0,
                    float(cmp_) if cmp_ is not None else 0.0,
                )
        return list(merged.values())

    # ------------------------------------------------------------------
    # 方法 3:detect_anomaly(日历感知,§4.4)
    # ------------------------------------------------------------------
    def detect_anomaly(self, metric, start, end, cmp_start, cmp_end,
                       threshold=0.15) -> dict:
        """日历感知的异常检测:由 attribution.anomaly.assess 判定(§4.4)。

        日序列取 [start - lookback, end](lookback = 基线周数 × 7 × 2,保证促销日历
        剔除后仍有等宽干净历史);判定走 assess(series, start, end, threshold, calendar),
        calendar = 语义层 time.calendar(未声明为 None)。返回 {"metric"} + assess 冻结键,
        **只增不改**。cmp_start/cmp_end 已弃用:基线由窗口前同星期几历史估计,参数保留
        仅为签名兼容。metric 不存在抛 SemanticError;日序列空/历史不足如实退化,不编基线。
        """
        self.semantic.metric(metric)    # 未知指标 -> SemanticError
        # 日期归一化:契约只承诺 YYYY-MM-DD,但 anomaly 模块对带时间分量的写法是容忍的——
        # 这里同口径截掉时间分量,避免带时分的字符串把窗口首日从 SQL 比较里挤掉。
        start = str(start).strip().split(" ")[0]
        end = str(end).strip().split(" ")[0]
        # 基线回看:对比窗口起点往前推 lookback 天;序列只取到 end(窗口之后的数据不参与)
        lookback = BASELINE_WEEKS_DEFAULT * _DAYS_PER_WEEK * _LOOKBACK_MARGIN
        series_start = datetime.strptime(start, _STAMP_FORMAT) - timedelta(days=lookback)
        series = self.source.daily_series(metric, series_start.strftime(_STAMP_FORMAT), end)
        return {"metric": metric, **assess(series, start, end, threshold,
                                           self.semantic.time_calendar)}

    # ------------------------------------------------------------------
    # 方法 4:decompose(LMDI 分解,§4.2)
    # ------------------------------------------------------------------
    def decompose(self, target, factors, base_start, base_end, cmp_start, cmp_end,
                  dimension=None, level=None, filters=None, top_k=5) -> dict:
        """LMDI 分解:把 target 的基期→对比期总变化拆成因子的效应,零残差。

        - (target, factors) 必须命中语义层 decompositions 声明(因子集合一致,顺序任意),
          未命中抛 SemanticError;kind 与 entity_dimension 以声明为准。
        - 不带 dimension/level 对全窗分解一次;带则逐切片分解(level 须在 dimension 的
          hierarchy 里,否则 ValueError),按 |total_change| 降序取前 top_k。
        - filters 同时作用于 target 与全部因子(与 query_metric 同口径)。
        - 取值口径:multiplicative / additive = 窗口内标量聚合;ratio = factors 恰两个,
          分子前分母后;structural 要求 target 是「分子/分母」形 derived 指标(泛化规则),
          权重 = 分母实体占比,强度 = 分子/分母,恒等式 V = Σ w·r 由分解模块校验,
          口径不符抛 DecomposeError。
        - 切片单侧缺失按 0 处理;因子非正 / total_change 为 0 等边界由
          attribution.decompose 处理(δ 替代并标注近似;该模块纯函数,只取数与整形)。

        返回(纯 dict,JSON 可序列化):
          {
            "target": str, "kind": str, "factors": [声明因子名],
            "dimension": str|None, "level": str|None,
            "total_base": float|None, "total_cmp": float|None, "total_change": float|None,
            "effects": [{"factor", "label", "base", "cmp", "effect", "contribution", "change_rate"}],
            "entity_changes": [   # 仅 structural:下架/新上实体(任一期分母为 0)单列
                {"entity", "label", "only_in", "numerator_base", "numerator_cmp",
                 "denominator_base", "denominator_cmp"}],
            "slices": [{"key", "label", "total_base", "total_cmp", "total_change", "effects": [...]}],
          }
        """
        # 实现主体在 attribution.engine_decompose(本文件有单文件行数约束);
        # 契约以本 docstring 为准,两者不得漂移。
        return decompose_for(
            self, target, list(factors), base_start, base_end, cmp_start, cmp_end,
            dimension=dimension, level=level, filters=dict(filters or {}) or None,
            top_k=top_k)
