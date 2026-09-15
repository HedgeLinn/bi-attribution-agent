# -*- coding: utf-8 -*-
"""生成 BI 归因分析示例数据(星型模型,落地为 Parquet)。

设计要点:
  - 时间跨度 24 个月(2024-09-01 ~ 2026-08-31),因此 2026-06 既可比环比(2026-05)
    也可比同比(2025-06)。
  - 固定随机种子 SEED=42 -> 完全可复现,每次运行结果一致。
  - 1 个主异常 + 1 个干扰项(618 季节性回落),归因答案唯一。

埋点异常(常量,供后续验证):
  * 主异常:华东 -> 上海 -> 上海徐家汇旗舰店(STORE_S0001),
    2026-06-01 起该店唯一头部高单价 SKU「SKU_P0001 旗舰智能手机 Pro Max」直接下架。
    预期:客单价暴跌、销量仅微降(GMV 约 -50%,销量约 -5%,客单价约 -45%)。
  * 干扰项:每 6 月整月 GMV 环比 5 月自然回落约 -10%(模拟 618 大促后消费透支),
    覆盖所有门店/地区,防止 agent 停在「整体下跌」就交差。
"""
import sys

import numpy as np
import pandas as pd
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.datasets import resolve_dataset  # noqa: E402
from scripts.generate_data_scenarios import apply_scenarios, builtin_report, write_outputs  # noqa: E402

# 本脚本只造 ecommerce-demo 这一个数据集的数据(M2,§3.3):
# 写入位置由数据集包决定(数据集包里的 data_dir),不再写死根目录的 data/
DATASET_ID = "ecommerce-demo"
DATA_DIR = resolve_dataset(DATASET_ID, datasets_dir=ROOT / "datasets").data_dir

SEED = 42
rng = np.random.default_rng(SEED)

# ================ 埋点异常:常量标注(主异常 + 干扰项) ================
ANOM_STORE_ID = "STORE_S0001"
ANOM_STORE_NAME = "上海徐家汇旗舰店"          # 华东 -> 上海
ANOM_SKU_ID = "SKU_P0001"
ANOM_SKU_NAME = "旗舰智能手机 Pro Max"        # 高单价头部 SKU
ANOM_START = date(2026, 6, 1)                  # 断崖起效日(含)
# 主异常店内占比设计(可在生成后核对):
#   ANOM_SKU 占该店订单行约 9% -> 下架后销量 -9%(微降)
#   同时该 SKU 客单价 8999 元,占该店 GMV 约 47% -> GMV -47%,客单价(GMV/订单数)约 -47%
ANOM_ROW_SHARE = 0.09

# ================ 维度:渠道 ================
DIM_CHANNEL = pd.DataFrame([
    # (channel_id, channel_name)
    ("CH_01", "线上-官方商城"),
    ("CH_02", "线上-淘宝"),
    ("CH_03", "线上-京东"),
    ("CH_04", "线上-抖音直播"),
    ("CH_05", "线上-拼多多"),
    ("CH_06", "线下-直营门店"),
    ("CH_07", "线下-加盟门店"),
    ("CH_08", "线下-商超专柜"),
], columns=["channel_id", "channel_name"])
CHANNEL_WEIGHTS = np.array([0.18, 0.22, 0.14, 0.09, 0.07, 0.14, 0.10, 0.06])  # 线上~70%

# ================ 维度:日期(2024-09-01 ~ 2026-08-31,24 个月) ================
DAY0 = date(2024, 9, 1)
DAY1 = date(2026, 8, 31)
dates = pd.date_range(DAY0, DAY1, freq="D")
ISO = "%Y-%m-%d"
dim_date_df = pd.DataFrame({
    "date_id": dates.strftime(ISO),
    "date": dates.strftime(ISO),  # 与 date_id 相同格式的字符串,短传亦可
    "year": dates.year.values,
    "month": dates.month.values,
    "day": dates.day.values,
    "week": dates.isocalendar().week.values,  # ISO 周
})

# 背景季节系数(月度连续,体现自然季节性;6 月为 618 后回落低谷)
SEASON = {1: 0.90, 2: 0.92, 3: 0.95, 4: 0.98, 5: 1.06, 6: 0.95,   # 6 月<5 月 -> 618 回落
          7: 0.90, 8: 0.93, 9: 1.00, 10: 1.05, 11: 1.20, 12: 1.25}
season_vals = dates.month.map(SEASON).to_numpy(dtype=float)

# 增长趋势:每月约 +0.6%,24 个月累计约 +15% 背景增长
months_idx = (dates.year - DAY0.year) * 12 + (dates.month - 1) - (DAY0.month - 1)
growth_vals = 1.0 + 0.006 * months_idx.to_numpy(dtype=float)

# 星期因子(周末高)
DOW = {0: 0.82, 1: 0.90, 2: 0.95, 3: 1.00, 4: 1.05, 5: 1.18, 6: 1.25}
dow_vals = dates.dayofweek.map(DOW).to_numpy(dtype=float)

# ================ 维度:门店(星型 4 维中的大维,不均匀分布) ================
REGION_CITY = {
    "华东": ["上海", "杭州", "南京", "苏州", "宁波", "合肥"],
    "华北": ["北京", "天津", "石家庄", "太原", "济南", "青岛"],
    "华南": ["广州", "深圳", "佛山", "厦门", "福州", "东莞"],
    "华中": ["武汉", "长沙", "郑州", "南昌"],
    "西南": ["成都", "重庆", "昆明", "贵阳"],
    "西北": ["西安", "兰州", "西宁", "乌鲁木齐"],
    "东北": ["沈阳", "大连", "长春", "哈尔滨"],
}
REGION_W = np.array([0.30, 0.20, 0.22, 0.12, 0.08, 0.05, 0.03])
regions_list = list(REGION_CITY.keys())

TIERS = {"flagship": 12.0, "center": 3.5, "community": 1.0}  # 店日均订单数基准

# 固定 5 家旗舰店(每大区中心旗舰),其中上海徐家汇旗舰店即异常店
FIXED_FLAGSHIPS = [
    ("STORE_S0001", "上海徐家汇旗舰店", "华东", "上海"),   # 异常店
    ("STORE_S0002", "北京国贸旗舰店", "华北", "北京"),
    ("STORE_S0003", "广州天河旗舰店", "华南", "广州"),
    ("STORE_S0004", "成都春熙路旗舰店", "西南", "成都"),
    ("STORE_S0005", "武汉江汉路旗舰店", "华中", "武汉"),
]

store_rows = []
store_idx = 1
for sid, sname, region, city in FIXED_FLAGSHIPS:
    store_rows.append((sid, sname, city, region, "旗舰店"))
    store_idx += 1

n_center = 45
n_community = 400
for _ in range(n_center + n_community):
    region = regions_list[rng.choice(len(REGION_W), p=REGION_W)]
    cities = REGION_CITY[region]
    # 城市权重(首城占 45%,旗舰所在城市更集中)
    cw = np.full(len(cities), (1 - 0.45) / (len(cities) - 1))
    cw[0] = 0.45
    city = cities[rng.choice(len(cities), p=cw)]
    tier = "center" if store_idx <= 1 + n_center else "community"
    tier_cn = {"center": "中心店", "community": "社区店"}[tier]
    sid = f"STORE_S{store_idx:04d}"
    suffix = rng.choice(["中心店", "购物中心店", "奥莱店"] if tier == "center" else
                        ["社区店", "街边店", "门店"])
    name = f"{city}{suffix}"
    store_rows.append((sid, name, city, region, tier_cn))
    store_idx += 1

dim_store_df = pd.DataFrame(store_rows, columns=["store_id", "store_name", "city", "region", "tier"])
# 按 id 排序,保持异常店等旗舰在最前
dim_store_df = dim_store_df.sort_values("store_id").reset_index(drop=True)
store_id_list = dim_store_df["store_id"].tolist()
store_index = {sid: i for i, sid in enumerate(store_id_list)}
# 店日均订单数
tier_base = dim_store_df["tier"].map({"旗舰店": TIERS["flagship"],
                                      "中心店": TIERS["center"],
                                      "社区店": TIERS["community"]}).to_numpy(dtype=float)
anom_store_pos = store_index[ANOM_STORE_ID]
anom_base = tier_base[anom_store_pos]

# ================ 维度:商品(SKU,不均匀:低价铺货、高价稀缺) ================
PRODUCT_CAT = [
    ("CAT01", "数码家电", [("SUB101", "手机通讯"), ("SUB102", "电脑办公"), ("SUB103", "大家电"), ("SUB104", "小家电")], 90),
    ("CAT02", "服饰鞋包", [("SUB201", "男装"), ("SUB202", "女装"), ("SUB203", "运动户外"), ("SUB204", "鞋靴")], 100),
    ("CAT03", "食品生鲜", [("SUB301", "粮油调味"), ("SUB302", "休闲零食"), ("SUB303", "生鲜水果"), ("SUB304", "饮料冲调")], 130),
    ("CAT04", "家居百货", [("SUB401", "家具家纺"), ("SUB402", "日用清洁"), ("SUB403", "厨具餐具"), ("SUB404", "个护美妆")], 100),
]
prod_rows = []
sku_idx = 1
ANOM_SKU_POS = None
for cat_id, cat_name, subs, n_sku in PRODUCT_CAT:
    for sub_id, sub_name in subs:
        for _ in range(max(1, n_sku // 4)):
            pid = f"SKU_P{sku_idx:04d}"
            price = float(rng.lognormal(mean=np.log(180), sigma=1.1))  # 长尾:大量低价 + 少数高价
            price = round(min(max(price, 9.9), 12000), 2)
            # 高单价头部 SKU 固定为 SKU_P0001
            if pid == "SKU_P0001":
                price = 8999.0
                sub_name_fixed = "手机通讯"
            else:
                sub_name_fixed = sub_name
            prod_rows.append((pid, f"{cat_name}{sub_name_fixed}-{sku_idx:03d}号", sub_name_fixed, cat_name, price))
            sku_idx += 1

dim_product_df = pd.DataFrame(prod_rows, columns=["product_id", "sku_name", "subcategory", "category", "base_price"])
dim_product_df = dim_product_df.sort_values("product_id").reset_index(drop=True)
product_id_list = dim_product_df["product_id"].tolist()
product_index = {pid: i for i, pid in enumerate(product_id_list)}
base_price_arr = dim_product_df["base_price"].to_numpy(dtype=float)
category_arr = dim_product_df["category"].to_numpy()
ANOM_SKU_POS = product_index[ANOM_SKU_ID]
assert ANOM_SKU_POS is not None and abs(base_price_arr[ANOM_SKU_POS] - 8999) < 1e-6
NPROD = len(product_id_list)

# 商品曝光权重(越便宜卖得越多);高价商品覆盖率低,仅在旗舰/中心店铺货
exposure_arr = 1.0 / (base_price_arr / 300.0 + 1.0)
# store-product 亲和噪声(让分布不均,贡献度下钻有意义)
store_affinity = rng.lognormal(mean=0.0, sigma=0.6, size=(len(store_id_list), NPROD))

def build_pool(store_pos):
    """返回该店的可售商品 id 与归一化权重(正常/下架后),并记录 ANOM_SKU 原始权重份额。

    高频低价 SKU 全店铺货;高价(不高于 2500)SKU 仅旗舰/中心店铺货,使各店商品结构不同。
    异常店加权数字码高单价盘,并把 ANOM_SKU 的订单行占比锚定为 ANOM_ROW_SHARE。
    """
    idx = []
    w = []
    for p_pos in range(NPROD):
        price = base_price_arr[p_pos]
        if price > 2500:
            cov_ok = tier_base[store_pos] >= TIERS["center"]
        elif price > 800:
            cov_ok = tier_base[store_pos] >= TIERS["community"] or rng.random() < 0.6
        else:
            cov_ok = True
        if not cov_ok:
            continue
        idx.append(p_pos)
        w.append(exposure_arr[p_pos] * store_affinity[store_pos, p_pos])
    idx = np.array(idx)
    w = np.array(w)
    # 异常店:加权码高单价盘 + 头部 SKU 行占比锚定
    if store_pos == anom_store_pos:
        keep = (category_arr[idx] == "数码家电") & (base_price_arr[idx] >= 400)
        idx = idx[keep]
        w = w[keep]
        anom_in = np.nonzero(idx == ANOM_SKU_POS)[0]
        if anom_in.size:
            w_others = w.sum() - w[anom_in[0]]
            w[anom_in[0]] = w_others * ANOM_ROW_SHARE / (1 - ANOM_ROW_SHARE)
    w_norm = w / w.sum()
    anom_w = 0.0
    w_drop = w.copy()
    pos = np.nonzero(idx == ANOM_SKU_POS)[0]
    if pos.size:
        anom_w = w_norm[pos[0]]
        w_drop[pos[0]] = 0.0
        w_drop = w_drop / w_drop.sum()  # 下架后的池归一化
    return idx, w_norm, w_drop, anom_w

pools = [build_pool(s) for s in range(len(store_id_list))]

# ================ 事实表:orders 生成 ================
ch_ids = DIM_CHANNEL["channel_id"].to_numpy()
ch_w = CHANNEL_WEIGHTS / CHANNEL_WEIGHTS.sum()
d0_np = dates.to_numpy()

rows_buf = []
global_order = 0
N_DAYS = len(dates)

print(f"生成订单明细: {N_DAYS} 天 x {len(store_id_list)} 家门店 ...")

for di, d in enumerate(dates):
    is_after_anom = (d.date() >= ANOM_START)  # 2026-06-01 起异常生效
    day_season = season_vals[di]
    day_growth = growth_vals[di]
    day_dow = dow_vals[di]
    for si in range(len(store_id_list)):
        lam = tier_base[si] * day_season * day_growth * day_dow
        n_orders = int(rng.poisson(lam))
        if n_orders == 0:
            continue
        rows_per_order = rng.choice([1, 2, 3], size=n_orders, p=[0.55, 0.32, 0.13])
        total_rows = int(rows_per_order.sum())
        pool_idx, w_norm, w_drop, _ = pools[si]
        w_use = w_drop if (is_after_anom and si == anom_store_pos) else w_norm
        picks_pos = rng.choice(pool_idx, size=total_rows, p=w_use, replace=True)
        # 订单内渠道一致
        ch_pick = rng.choice(ch_ids, size=n_orders, p=ch_w)
        order_idx_in_batch = np.repeat(np.arange(n_orders), rows_per_order)
        ch_arr = ch_pick[order_idx_in_batch]
        qty_arr = rng.choice([1, 1, 1, 2, 2, 3], size=total_rows)
        price_arr = base_price_arr[picks_pos] * (1.0 + rng.normal(0, 0.02, size=total_rows))
        amount_arr = np.round(price_arr * qty_arr, 2)
        # 折扣(约 28% 的订单行为优惠行,优惠率 0~15%)
        d_mask = rng.random(total_rows) < 0.28
        disc_arr = np.where(d_mask, np.round(amount_arr * rng.uniform(0, 0.15, size=total_rows), 2), 0.0)
        # 退款(约 2% 订单行退款,退 30%~100%)
        r_mask = rng.random(total_rows) < 0.02
        ref_arr = np.where(r_mask, np.round(amount_arr * rng.uniform(0.3, 1.0, size=total_rows), 2), 0.0)

        n_batch_end = global_order + n_orders
        order_ids = np.arange(global_order, n_batch_end)
        order_ids_arr = np.repeat(order_ids, rows_per_order).astype(object)
        oid_str = np.array([f"ORD_{o:08d}" for o in order_ids_arr], dtype=object)
        date_str = np.full(total_rows, d.strftime(ISO), dtype=object)
        store_str = np.full(total_rows, store_id_list[si], dtype=object)
        prod_str = np.array([product_id_list[p] for p in picks_pos], dtype=object)

        rows_buf.extend(zip(oid_str, date_str, store_str, prod_str, ch_arr, qty_arr,
                            amount_arr, disc_arr, ref_arr))
        global_order = n_batch_end

print(f"生成完毕: 共 {len(rows_buf)} 行订单明细 / {global_order} 个订单")

orders_df = pd.DataFrame(rows_buf, columns=[
    "order_id", "date_id", "store_id", "product_id", "channel_id",
    "quantity", "amount", "discount", "refund"])

# ================ §5.3 场景埋点 + 写盘 + 内置验证 ================
# 场景埋点一律在主循环**之后**施加,只动与既有埋点不相交的 (门店 × 时间窗);
# 详细设计与窗口清单见 scripts/generate_data_scenarios.py
orders_df, dim_store_df, global_order = apply_scenarios(orders_df, dim_store_df, global_order)
write_outputs(DATA_DIR, dict(dim_date=dim_date_df, dim_store=dim_store_df,
                             dim_product=dim_product_df, dim_channel=DIM_CHANNEL, orders=orders_df))
builtin_report(DATA_DIR, ANOM_STORE_ID, ANOM_STORE_NAME)
