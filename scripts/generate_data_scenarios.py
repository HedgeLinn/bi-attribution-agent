# -*- coding: utf-8 -*-
"""ecommerce-demo 造数的「收尾」与「场景埋点」:写盘、内置验证、§5.3 场景后处理。

为什么单独一个文件:
  `generate_data.py` 已接近 300 行上限(仓库编码规范:单文件 ≤300 行)。把
  「主循环之后」的部分搬到这里,主脚本就只剩**基础数据生成**,增量场景埋点也
  集中在本文件,互不干扰。

**行为不变性是硬约束(重构红线)**:本文件里 `write_outputs` / `builtin_report`
是从 `generate_data.py` **原样搬家**,不得改变任何写盘或打印行为——拆分前后
`data/*.parquet` 必须逐字节相同(以 md5 证明)。

场景埋点(§5.3,由 `apply_scenarios` 追加)一律在既有主循环**全部跑完之后**施加,
且只作用于**与既有埋点不相交的 (门店 × 时间窗)**;它们必须使用自己的独立随机源,
**绝不消耗主循环的 rng 抽样序列**——否则 2026-06 全量 GMV 等被测试钉死的数值会漂移。

排查顺序建议:先 `python scripts/generate_data.py`,再 `python scripts/verify_data.py`
与 `python attribution/self_test.py`——三者都是离线可跑的。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ref(data_dir: Path, table: str) -> str:
    """表名 -> read_parquet 的路径(正斜杠,避免 Windows 反斜杠转义)。"""
    return (Path(data_dir) / f"{table}.parquet").as_posix()


# ===========================================================================
# §5.3 场景埋点(E1/E2/E4/E5/E6):全部追加在既有主循环**之后**
# ===========================================================================
# 铁律:不得改变任何已被测试钉死的数值(2026-06 全量 GMV、2025-12/2026-01 全量、
# STORE_S0001 / SKU_P0001、618 干扰项口径、CASE_A/CASE_B 的 detect_anomaly…)。
# 因此本节的每一条都满足:
#   ① 只作用于**新窗口**——一律落在 2024-09-03~2025-11-05 这段「免费区间」内:它避开
#      了被钉死的 2026-05 / 2026-06 / 2025-12~2026-01 与 2024-09-01~02(decompose 切片数
#      测试),也避开 detect_anomaly 的 56 天回看区间(CASE_A 的序列自 2026-04-06 起、
#      c4 的自 2025-11-06 起 → 本节的窗口连基线候选点都不进);
#   ② 只作用于**指定的门店 / 城市**——新增门店用从未出现过的 STORE_S04xx;
#   ③ 全程**按整单操作**(复制、丢弃都以 order_id 为单位),保持原数据
#      「一个 order_id 只属于一家店 + 一天」的不变量;
#   ④ 不改动既有行的数量(除 E1 有意删单、E4/E5 有意复制单),也不消耗主循环的 rng。

SCENARIO_SEED = 20250913        # 独立随机源(当前各场景都是确定性抽样,留作扩展用)
# 窗口选择的另一条讲究:尽量让「对比期」与「基准期」**天数相同**,免得埋点的变化率
# 被 28/30/31 天的口径差污染(那是 c3 的考点,不该混进来)。
# 免费区间(见上)内天数相同的相邻月份对只有 (2024-12→2025-01) 与 (2025-07→2025-08);
# 前者夹着 c4 的季节断崖,故 E1/E2 用 2025-07、E4/E6 用 2025-08、E5 用 2025-04
# (2025-04 的天数差 -3.2% 恰好被季节 +3.2% 抵消)。
# E5 的城市是**实测挑的**:要「存量门店自然环比 ≈ 0」才能让「结构变化」成为唯一解释。
# 各城市 2025-03→04 存量店的天然环比里,合肥 +2.6%(3 月 427,118 / 4 月 438,094)最接近 0;
# 苏州 -18.9%(小城市 + 门店集中 -> 抽样波动大),会把「存量下滑」混进答案里,故不用。

# E1 纯量效应:北京国贸旗舰店 2025-07(基准 2025-06)
E1_STORE, E1_WINDOW = "STORE_S0002", ("2025-07-01", "2025-07-31")
E1_KEEP_EVERY = 2               # 隔一个整单保留一个 -> 订单数 ×0.5

# E2 纯价效应:广州天河旗舰店 2025-07(基准 2025-06;与 E1 同窗、不同城不同店)
E2_STORE, E2_WINDOW = "STORE_S0003", ("2025-07-01", "2025-07-31")
E2_PRICE_SCALE = 0.7            # 每行金额 ×0.7,订单/行数不动

# E4 量价反向:成都春熙路旗舰店 2025-08(基准 2025-07)
E4_STORE, E4_WINDOW = "STORE_S0004", ("2025-08-01", "2025-08-31")
E4_DUP_EVERY, E4_DUP_TAKE = 5, 2        # 每 5 个整单复制 2 个 -> 订单数 ×1.4
E4_PRICE_SCALE = 1.0 / 1.4              # 金额 ×1/1.4 -> GMV 恰好大致持平

# E5 门店权重结构变化:合肥 2025-04 凭空多 1 家新店(基准 2025-03)
# 只开 1 家(而不是 3 家)是为了让 case 的 root_cause_slice 保持**单键**可判 ——
# 评分是 `conclusion["根因"]["key"] == expected.root_cause_slice.key` 的严格相等,
# 3 家新店作为共同根因没有单一 key 能表达,写哪一家都会误判另外两家。
E5_CITY, E5_WINDOW = "合肥", ("2025-04-01", "2025-04-30")
E5_TAKE_EVERY, E5_TAKE = 100, 22        # 合肥存量店每 100 个整单取 22 个复制给新店
E5_NEW_STORE = "STORE_S0451"
E5_NEW_NAME = "合肥政务区中心店"
E5_NEW_TIER = "中心店"

# E6 优惠率上升(隐性价降):武汉江汉路旗舰店 2025-08(基准 2025-07;与 E4 同窗不同城)
E6_STORE, E6_WINDOW = "STORE_S0005", ("2025-08-01", "2025-08-31")
E6_DISCOUNT_SCALE = 2.0         # discount 列 ×2 -> discount_rate 翻倍,GMV 一字不动


def apply_scenarios(orders, dim_store, order_seq):
    """在既有主循环跑完之后施加 §5.3 场景埋点;返回 (orders, dim_store, order_seq)。

    order_seq 是主循环统计出的订单总数(E4/E5 复制整单时从这里往后发新单号)。
    """
    orders = _e1_pure_volume(orders)
    orders = _e2_pure_price(orders)
    orders, order_seq = _e4_volume_price_reversal(orders, order_seq)
    orders, dim_store, order_seq = _e5_new_stores(orders, dim_store, order_seq)
    orders = _e6_hidden_discount(orders)
    orders, dim_store = orders.reset_index(drop=True), dim_store.reset_index(drop=True)
    print(f"场景埋点(E1/E2/E4/E5/E6)后: {len(orders)} 行 / {order_seq} 个订单 / "
          f"{len(dim_store)} 家门店")
    return orders, dim_store, order_seq


def _in_window(orders, store_ids, window):
    """布尔掩码:门店属于 store_ids 且 date_id 落在闭区间 window 内。"""
    lo, hi = window
    return (orders["store_id"].isin(store_ids)
            & (orders["date_id"] >= lo) & (orders["date_id"] <= hi))


def _window_order_ids(orders, store_ids, window):
    """窗口内该批门店的整单号(升序,保证抽样可复现)。"""
    return (orders.loc[_in_window(orders, store_ids, window), "order_id"]
            .drop_duplicates().sort_values().reset_index(drop=True))


def _mint(blocks, order_seq):
    """给一批整单号分配全新单号:原单号 -> ORD_{order_seq+i:08d};返回 (映射, 新计数)。"""
    mapping = {old: f"ORD_{order_seq + i:08d}" for i, old in enumerate(blocks)}
    return mapping, order_seq + len(mapping)


def _copy_orders(orders, picked_ids, mapping, store_map=None):
    """把 picked_ids 这些整单的行整块复制出来,改发新单号;给了 store_map 就改挂门店。"""
    copied = orders[orders["order_id"].isin(set(picked_ids))].copy()
    copied["order_id"] = copied["order_id"].map(mapping)
    if store_map is not None:
        copied["store_id"] = copied["store_id"].map(store_map)
    return copied


def _e1_pure_volume(orders):
    """E1 纯量效应:窗口内隔单保留一半整单,每行金额一字不动 -> 客单价不动。

    GMV 的降幅 100% 来自「量」(订单数/销量),「价」(客单价)纹丝不动。
    """
    order_ids = _window_order_ids(orders, [E1_STORE], E1_WINDOW)
    keep = set(order_ids.iloc[::E1_KEEP_EVERY])
    drop = _in_window(orders, [E1_STORE], E1_WINDOW) & ~orders["order_id"].isin(keep)
    return orders.loc[~drop]


def _e2_pure_price(orders):
    """E2 纯价效应:窗口内每行金额 ×0.7,订单数与行数不动 -> 客单价 ×0.7。

    GMV 的降幅 100% 来自「价」,「量」纹丝不动 —— 与 E1 恰成一对。
    """
    out = orders.copy()
    mask = _in_window(out, [E2_STORE], E2_WINDOW)
    for column in ("amount", "discount", "refund"):
        out.loc[mask, column] = (out.loc[mask, column] * E2_PRICE_SCALE).round(2)
    return out


def _e4_volume_price_reversal(orders, order_seq):
    """E4 量价反向:复制 ~40% 整单(发新单号),再把整窗金额 ×1/1.4。

    订单数 ×1.4、客单价 ×0.714,两者相乘使 GMV 恰好大致持平 —— 总量看不出问题,
    但结构在劣化(靠走量撑住同样的盘子)。
    """
    order_ids = _window_order_ids(orders, [E4_STORE], E4_WINDOW)
    position = np.arange(len(order_ids))
    dup_ids = order_ids.iloc[position[position % E4_DUP_EVERY < E4_DUP_TAKE]]
    mapping, order_seq = _mint(dup_ids.tolist(), order_seq)
    copied = _copy_orders(orders, dup_ids, mapping)
    out = pd.concat([orders, copied], ignore_index=True)
    mask = _in_window(out, [E4_STORE], E4_WINDOW)
    for column in ("amount", "discount", "refund"):
        out.loc[mask, column] = (out.loc[mask, column] * E4_PRICE_SCALE).round(2)
    return out, order_seq


def _e5_new_stores(orders, dim_store, order_seq):
    """E5 门店权重结构变化:把合肥存量店窗口内 ~22% 的整单复制到 1 家新门店。

    存量门店的行一字未动(零改动),城市总量的跳变**全部来自新增门店** —— 即「结构
    变化」而不是「存量增长」:这家店的基准期是「不存在」(引擎返回 None),不是「有值但低」。
    """
    city_stores = dim_store.loc[dim_store["city"] == E5_CITY, "store_id"].tolist()
    order_ids = _window_order_ids(orders, city_stores, E5_WINDOW)
    position = np.arange(len(order_ids))
    taken = order_ids.iloc[position[position % E5_TAKE_EVERY < E5_TAKE]].tolist()
    if not taken:
        return orders, dim_store, order_seq
    mapping, order_seq = _mint(sorted(taken), order_seq)
    copied = _copy_orders(orders, taken, mapping, {s: E5_NEW_STORE for s in city_stores})
    dim_store = pd.concat([dim_store, pd.DataFrame([{
        "store_id": E5_NEW_STORE, "store_name": E5_NEW_NAME, "city": E5_CITY,
        "region": dim_store.loc[dim_store["city"] == E5_CITY, "region"].iloc[0],
        "tier": E5_NEW_TIER}])], ignore_index=True)
    return pd.concat([orders, copied], ignore_index=True), dim_store, order_seq


def _e6_hidden_discount(orders):
    """E6 优惠率上升(隐性价降):窗口内 discount 列 ×2。

    GMV 口径是 SUM(amount)、不含折扣,所以**总额看不出任何问题**;
    只有 discount_rate(= SUM(discount)/SUM(gmv))翻倍这一个指标能显形 ——
    考的是「换一个指标交叉验证」而不是只看 GMV。
    """
    out = orders.copy()
    mask = _in_window(out, [E6_STORE], E6_WINDOW)
    out.loc[mask, "discount"] = (out.loc[mask, "discount"] * E6_DISCOUNT_SCALE).round(2)
    return out


# ---------------------------------------------------------------------------
# 写盘:五张表落 Parquet
# ---------------------------------------------------------------------------
def write_outputs(data_dir, frames: dict) -> None:
    """把五张表写入数据集包的 data 目录(键名即表名,顺序无关)。"""
    data_dir = Path(data_dir)
    data_dir.mkdir(exist_ok=True, parents=True)
    for table in ("dim_date", "dim_store", "dim_product", "dim_channel", "orders"):
        frames[table].to_parquet(data_dir / f"{table}.parquet", index=False)
    print(f"Parquet 已写入 {data_dir}")


# ---------------------------------------------------------------------------
# 内置验证:直接查刚写出的 parquet(只需 duckdb)
# ---------------------------------------------------------------------------
def builtin_report(data_dir, anom_store_id: str, anom_store_name: str) -> None:
    """造数脚本自带的对照报告:全量 / 主异常店 / 同地区 / 同城市 2026-05 vs 06。"""
    import duckdb  # 局部导入:只有跑到验证阶段才需要

    con = duckdb.connect()
    con.execute(f"""
        CREATE VIEW orders AS SELECT * FROM read_parquet('{_ref(data_dir, 'orders')}');
        CREATE VIEW ds AS SELECT * FROM read_parquet('{_ref(data_dir, 'dim_store')}');
        CREATE VIEW dp AS SELECT * FROM read_parquet('{_ref(data_dir, 'dim_product')}');
        CREATE VIEW dd AS SELECT * FROM read_parquet('{_ref(data_dir, 'dim_date')}');
    """)

    print("\n==================== 内置验证 ====================")
    print("orders 行数:", con.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
    print("各维度行数:", con.execute(
        "SELECT (SELECT COUNT(*) FROM ds), (SELECT COUNT(*) FROM dp), (SELECT COUNT(*) FROM dd),"
        f" (SELECT COUNT(*) FROM read_parquet('{_ref(data_dir, 'dim_channel')}'))"
    ).fetchone())

    def month_gmv(extra_where=""):
        return con.execute(f"""
            SELECT r.month, SUM(o.amount) AS gmv, SUM(o.quantity) AS qty,
                   COUNT(DISTINCT o.order_id) AS ords
            FROM orders o JOIN dd r ON o.date_id = r.date_id JOIN ds s ON o.store_id = s.store_id
            WHERE r.year = 2026 AND r.month IN (5, 6) {extra_where}
            GROUP BY r.month ORDER BY r.month
        """).fetchall()

    print("\n[全量] 2026-05 vs 2026-06(618 回落,干扰项):")
    all_res = month_gmv()
    print("   05:", [f"{v:.0f}" for v in all_res[0][1:]])
    print("   06:", [f"{v:.0f}" for v in all_res[1][1:]])
    g1, g2 = all_res[0][1], all_res[1][1]
    print(f"   GMV 环比: {(g2 / g1 - 1) * 100:+.1f}%")

    def store_month_gmv(extra_where, label):
        rows = month_gmv(extra_where)
        m5, m6 = rows[0], rows[1]
        gmv_chg = (m6[1] / m5[1] - 1) * 100
        qty_chg = (m6[2] / m5[2] - 1) * 100
        aov5, aov6 = m5[1] / m5[3], m6[1] / m6[3]
        aov_chg = (aov6 / aov5 - 1) * 100
        print(f"\n[{label}] 2026-05 vs 2026-06:")
        print(f"   GMV  05={m5[1]:.0f}  06={m6[1]:.0f}  环比 {gmv_chg:+.1f}%")
        print(f"   销量 05={m5[2]:.0f}  06={m6[2]:.0f}  环比 {qty_chg:+.1f}%")
        print(f"   客单价 AOV 05={aov5:.0f}  06={aov6:.0f}  环比 {aov_chg:+.1f}%")

    store_month_gmv(f"AND o.store_id = '{anom_store_id}'", f"主异常门店 {anom_store_name}")
    store_month_gmv(f"AND s.region = '华东' AND o.store_id != '{anom_store_id}'",
                    "同地区正常门店(华东,排除异常店)")
    store_month_gmv(f"AND s.city = '上海' AND o.store_id != '{anom_store_id}'",
                    "同城市正常门店(上海,排除异常店)")

    con.close()
    print("\n==================== 内置验证结束 ====================")
