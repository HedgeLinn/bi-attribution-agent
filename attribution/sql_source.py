"""数据访问层:DuckDB 的列探测、SQL 构建与执行(Repository)。

边界(docs/REUSE_DESIGN.md §3.2;工作区规范「Repository Pattern:数据访问抽象」):
    - 本模块只认「表 / 字段 / SQL」:parquet 路径、表别名、JOIN 推导、聚合执行
    - 本模块**不认识归因算法**:贡献度、异常阈值、排序、数值整形一律留在 attribution.engine
    - 语义层在这里只作为「表名 / 字段名 / 维度表」的来源被读取,不做业务判断

对外暴露:introspect_columns(data_dir) 与 SqlSource(data_dir, semantic)
(aggregate / scalar / slice_rows / daily_series / fact_for,见各自 docstring)。

所有方法只返回 Python 原生结构(dict / tuple / 数值),不向上层泄漏连接或 DataFrame;
数值的取整、NaN -> None 规范化由调用方(engine 的结果整形层)负责。
"""

import contextlib
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb

from attribution.semantic import AGG_LAST, DIM_DERIVED, TYPE_SEMI_ADDITIVE, Semantic, SemanticError

# 事实表在 SQL 里的固定别名;维度表别名由维度名推导(d_<维度名>,见 field_ref / dim_ref)
_FACT_ALIAS = "o"


@contextlib.contextmanager
def _connect() -> Iterator["duckdb.DuckDBPyConnection"]:
    """开一个即用即关的 DuckDB 连接:数据源不跨调用持有连接。"""
    con = duckdb.connect()
    try:
        yield con
    finally:
        con.close()


def introspect_columns(data_dir: str) -> dict[str, frozenset[str]]:
    """扫描 data_dir 下的 parquet,返回 表名 -> 列名集合。

    供语义层编译指标表达式时识别裸列名、并做可达性校验(§3.6④)。
    """
    columns: dict[str, frozenset[str]] = {}
    with _connect() as con:
        for path in sorted(Path(data_dir).glob("*.parquet")):
            ref = path.as_posix().replace("'", "''")
            rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{ref}')").fetchall()
            columns[path.stem] = frozenset(str(row[0]) for row in rows)
    return columns


class SqlSource:
    """DuckDB 数据源(Repository):把「指标 + 维度 + 过滤 + 时间窗」翻译成 SQL 并执行。

    只负责「怎么取数」,不负责「取出来的数怎么解释」——后者是 AttributionEngine。
    无缓存:每次查询开一个即用即关的连接。
    """

    def __init__(self, data_dir: str, semantic: Semantic) -> None:
        self.data_dir = data_dir
        self.fact = semantic.fact_table
        self.date_field = semantic.date_field
        self._semantic = semantic

    # ------------------------------------------------------------------
    # SQL 片段构建
    # ------------------------------------------------------------------
    def parquet_ref(self, table: str) -> str:
        """表名 -> read_parquet(...) 引用(路径统一正斜杠,避免 Windows 反斜杠转义)。"""
        path = os.path.join(self.data_dir, f"{table}.parquet")
        return f"read_parquet('{path.replace(os.sep, '/')}')"

    def agg_expr(self, metric: str, fact_alias: str = _FACT_ALIAS) -> str:
        """指标的聚合表达式(由语义层编译),如 SUM(o.<事实表列>)。"""
        return self._semantic.agg_expr(metric, alias=fact_alias)

    def field_ref(self, field: str) -> str:
        """返回字段的限定引用(带表别名),自动判断命中的维度表;派生维度字段在事实表上。"""
        dname = self._semantic.field_to_dimension(field)
        if dname and self._semantic.dimension(dname).kind != DIM_DERIVED:
            return f"d_{dname}.{field}"
        return f"{_FACT_ALIAS}.{field}"

    def dim_ref(self, dimension: str, field: str) -> str:
        """维度表字段的限定引用(用于不在 hierarchy 里的展示列,如 name_column)。"""
        return f"d_{dimension}.{field}"

    def needed_joins(self, fields: Iterable[str]) -> set[str]:
        """fields 里落在某维度 hierarchy 的字段,join 该维度表;派生维度不 join(字段在事实表上)。"""
        found = {self._semantic.field_to_dimension(f) for f in fields}
        return {d for d in found if d and
                self._semantic.dimension(d).kind != DIM_DERIVED}

    def fact_for(self, metric: str) -> str:
        """该指标的事实表(metric.source 优先;默认 fact_table)——多事实表数据集(§3.2)
        里指标取自哪张表由语义层说了算。"""
        return self._semantic.metric(metric).source or self.fact

    def from_where(
        self,
        join_dims: Iterable[str],
        filters: Mapping[str, Any],
        start: str,
        end: str,
        table: str | None = None,
    ) -> tuple[str, list[Any]]:
        """构建 FROM('事实表 + 所需维度表 join') 与 WHERE(时间窗 + 过滤器);返回 (from_sql, params)。
        table 指定事实表(缺省默认);日期字段约定所有事实表同名。"""
        sql = f" FROM {self.parquet_ref(table or self.fact)} {_FACT_ALIAS}"
        for dname in join_dims:
            dim = self._semantic.dimension(dname)
            sql += (
                f" JOIN {self.parquet_ref(dim.table)} d_{dname}"
                f" ON {_FACT_ALIAS}.{dim.key} = d_{dname}.{dim.key}"
            )

        where = [f"{_FACT_ALIAS}.{self.date_field} >= ?",
                 f"{_FACT_ALIAS}.{self.date_field} <= ?"]
        params: list[Any] = [start, end]
        for k, v in filters.items():
            where.append(f"{self.field_ref(k)} = ?")
            params.append(v)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql, params

    # ------------------------------------------------------------------
    # 查询执行
    # ------------------------------------------------------------------
    def _semi_kind(self, metric: str) -> str | None:
        """半可加且按「末日值」聚合则返回 AGG_LAST,否则 None。

        半可加的查询语义只有 last 已实现(§5.6 的 MRR / DAU 场景:窗口内取**最后
        有数据日**的值而不是求和——求和是 §1.3 的静默错误)。其它取值(avg / max)
        尚未有查询语义,抛 SemanticError 宁败不静默按 sum 算错。
        """
        metric_def = self._semantic.metric(metric)
        if metric_def.type != TYPE_SEMI_ADDITIVE:
            return None
        if metric_def.time_aggregation != AGG_LAST:
            raise SemanticError(f"半可加指标 {metric} 的 time_aggregation "
                                f"'{metric_def.time_aggregation}' 尚未支持(当前只实现 last)")
        return AGG_LAST

    def _aggregate_semi_last(self, metric: str, group_cols: list[tuple[str, str]],
                             filters: Mapping[str, Any], start: str, end: str
                             ) -> list[dict[str, Any]]:
        """半可加(last)聚合:所有分组取**同一基准日** = 窗口内最后有数据日(全局),
        保证 Σ分组 == 总量(切片与聚合透镜自洽);基准日无数据的组不出现。"""
        date_ref = f"{_FACT_ALIAS}.{self.date_field}"
        agg = self.agg_expr(metric) + " AS v"
        join_dims = self.needed_joins([f for f, _ in group_cols] + list(filters.keys()))
        from_sql, params = self.from_where(join_dims, filters, start, end,
                                            table=self.fact_for(metric))
        inner = (f"SELECT {agg}, {date_ref} AS _sd"
                 + (", " + ", ".join(f"{ref} AS {key}" for key, ref in group_cols) if group_cols else "")
                 + f"{from_sql}"
                 + f" GROUP BY {date_ref}"
                 + (", " + ", ".join(ref for _, ref in group_cols) if group_cols else "")
                 + " HAVING v IS NOT NULL")
        if group_cols:
            keys = ", ".join(key for key, _ in group_cols)
            sql = (f"WITH d AS ({inner}) SELECT {keys}, v FROM d "
                   f"WHERE _sd = (SELECT MAX(_sd) FROM d)")
        else:
            sql = f"WITH d AS ({inner}) SELECT v FROM d WHERE _sd = (SELECT MAX(_sd) FROM d)"
        with _connect() as con:
            return con.execute(sql, params).fetchdf().to_dict("records")

    def aggregate(
        self,
        metric: str,
        dims: Sequence[str],
        filters: Mapping[str, Any],
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        """通用聚合查询。

        dims: 分组字段;为空表示整窗标量聚合(结果为单行)。返回 [{分组字段: 取值..., "v": 值}],
        行序不保证稳定。半可加指标分派到 _aggregate_semi_last,统一挡住 §1.3 的静默错误。
        """
        dims = list(dims)
        if self._semi_kind(metric):
            return self._aggregate_semi_last(
                metric, [(d, self.field_ref(d)) for d in dims], filters, start, end)
        join_dims = self.needed_joins(dims + list(filters.keys()))
        group_cols = [(d, self.field_ref(d)) for d in dims]
        agg = self.agg_expr(metric) + " AS v"
        from_sql, params = self.from_where(join_dims, filters, start, end,
                                            table=self.fact_for(metric))
        if group_cols:
            selected = ", ".join(f"{ref} AS {key}" for key, ref in group_cols)
            sql = (f"SELECT {agg}, {selected}{from_sql}"
                   f" GROUP BY {', '.join(ref for _, ref in group_cols)}")
        else:
            sql = f"SELECT {agg}{from_sql}"
        with _connect() as con:
            return con.execute(sql, params).fetchdf().to_dict("records")

    def scalar(
        self,
        metric: str,
        filters: Mapping[str, Any],
        start: str,
        end: str,
    ) -> Any:
        """整窗标量聚合(无分组):返回单行聚合值(可能为 NaN / None);
        半可加指标返回窗口内最后有数据日的值,无数据返回 None。"""
        if self._semi_kind(metric):
            rows = self._aggregate_semi_last(metric, [], filters, start, end)
            return rows[0]["v"] if rows else None
        return self.aggregate(metric, [], filters, start, end)[0]["v"]

    def daily_series(self, metric: str, start: str, end: str) -> list[tuple[str, float]]:
        """指标在 [start, end] 内的日序列,按日期升序;供异常检测做稳健基线(§4.4)。

        - 直接按**事实表日期字段**分组,不 join 维度表(日序列不需要维度展示列)
        - 缺失日期不出现在结果里(没有行不等于 0,见 §5.4 的 C4)
        - 返回 [(日期 'YYYY-MM-DD', 值)];日期用事实表原始取值(字符串)
        """
        date_ref = f"{_FACT_ALIAS}.{self.date_field}"
        sql = (
            f"SELECT {date_ref} AS d, {self.agg_expr(metric)} AS v"
            f" FROM {self.parquet_ref(self.fact_for(metric))} {_FACT_ALIAS}"
            f" WHERE {date_ref} >= ? AND {date_ref} <= ?"
            f" GROUP BY {date_ref} ORDER BY {date_ref}"
        )
        with _connect() as con:
            rows = con.execute(sql, [start, end]).fetchall()
        # 整组无有效值时聚合结果是 NULL:保持 None 而不是编一个 0,
        # 缺失由消费方(anomaly.assess)按「没有行不等于 0」的口径剔除(§5.4)
        return [(str(stamp), float(value) if value is not None else None)
                for stamp, value in rows]

    def slice_rows(
        self,
        metric: str,
        dimension: str,
        level: str,
        base_window: tuple[str, str],
        cmp_window: tuple[str, str],
        filters: Mapping[str, Any] | None = None,
    ) -> list[tuple]:
        """两段窗口各自按 level 分组聚合,再按 (key, label) FULL OUTER JOIN 配对。

        返回 [(label, key, base, cmp)];单侧切片缺失一侧为 None;
        半可加指标每个切片取窗口内最后有数据日的值。
        filters 与 query_metric 同口径(过滤字段用 ID),同时作用于两侧窗口。
        """
        filters = dict(filters or {})
        key_ref = self.field_ref(level)
        label_ref = self._label_ref(dimension, level)
        agg = self.agg_expr(metric)
        # join 集合按 level 字段推导(needed_joins 会排除派生维度——其层级字段在事实表上);
        # filters 的键同样可能要 join 维度表(过滤字段落在哪张维度表由 field_ref 决定)
        join_dims = self.needed_joins([level, *filters])
        from_base, base_params = self.from_where(join_dims, filters, *base_window,
                                                  table=self.fact_for(metric))
        from_cmp, cmp_params = self.from_where(join_dims, filters, *cmp_window,
                                              table=self.fact_for(metric))
        if self._semi_kind(metric):
            date_ref = f"{_FACT_ALIAS}.{self.date_field}"
            # 半可加:所有切片取同一基准日 = 窗口内最后有数据日(与总量透镜自洽;
            # 基准日没有该切片数据的切片不出现,缺失侧交给 FULL JOIN 记 None)
            def side(from_sql: str) -> str:
                return (f"SELECT k, lb, v FROM (WITH d AS (SELECT {key_ref} AS k, "
                        f"{label_ref} AS lb, {agg} AS v, {date_ref} AS _sd{from_sql} "
                        f"GROUP BY {key_ref}, {label_ref}, {date_ref} HAVING v IS NOT NULL) "
                        f"SELECT k, lb, v FROM d WHERE _sd = (SELECT MAX(_sd) FROM d))")
            base_side, cmp_side = side(from_base), side(from_cmp)
            sql = (
                f"WITH b AS ({base_side}), c AS ({cmp_side}) "
                f"SELECT COALESCE(b.lb, c.lb) AS label, COALESCE(b.k, c.k) AS key, "
                f"       b.v AS base, c.v AS cmp "
                f"FROM b FULL OUTER JOIN c ON b.k = c.k AND b.lb = c.lb"
            )
        else:
            sql = (
                f"WITH "
                f"b AS (SELECT {key_ref} AS k, {label_ref} AS lb, "
                f"      {agg} AS v{from_base} GROUP BY {key_ref}, {label_ref}), "
                f"c AS (SELECT {key_ref} AS k, {label_ref} AS lb, "
                f"      {agg} AS v{from_cmp} GROUP BY {key_ref}, {label_ref}) "
                f"SELECT COALESCE(b.lb, c.lb) AS label, COALESCE(b.k, c.k) AS key, "
                f"       b.v AS base, c.v AS cmp "
                f"FROM b FULL OUTER JOIN c ON b.k = c.k AND b.lb = c.lb"
            )
        with _connect() as con:
            return con.execute(sql, base_params + cmp_params).fetchall()

    def entity_labels(self, dimension: str) -> dict:
        """实体 key -> 展示名:读维度表的 key + name_column,供结构分解列「下架/新上」实体。

        全部字段来自语义层(不写死任何数据集词汇);name_column 未声明或读不到时返回空 dict,
        调用方回退用 key 本身作展示名。
        """
        dim = self._semantic.dimension(dimension)
        if not dim.name_column:
            return {}
        sql = (
            f"SELECT {self.dim_ref(dimension, dim.key)} AS k, "
            f"{self.dim_ref(dimension, dim.name_column)} AS l "
            f"FROM {self.parquet_ref(dim.table)} d_{dimension}"
        )
        try:
            with _connect() as con:
                return {k: l for k, l in con.execute(sql).fetchall()
                        if k is not None and l is not None}
        except duckdb.Error:
            return {}

    def _label_ref(self, dimension: str, level: str) -> str:
        """切片展示列的限定引用。

        规则:key 层用维度的 name_column(未声明则用 level 值本身);中间层用字段值本身。
        name_column 不在 hierarchy 里,故不能走 field_ref,只能按维度表限定。
        """
        dim = self._semantic.dimension(dimension)
        if level == dim.key and dim.name_column:
            return self.dim_ref(dimension, dim.name_column)
        return self.field_ref(level)
