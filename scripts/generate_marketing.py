# -*- coding: utf-8 -*-
"""生成 marketing-funnel 数据集(M5 第二套,也是最大的一套:14 case)。

设计要点(docs/REUSE_DESIGN.md §5.4 坑表 / §5.10 造数约束):
  - 2026-03-01 ~ 2026-08-31。**三套口径并存**是这套数据的灵魂:平台侧 ad_daily(默认
    事实表)/ 埋点侧 touchpoints / 业务侧 orders,连同 5 张维表共 8 张表,口径不可混用。
  - 可达性校验只看默认事实表,故日期字段与全部维度 key 都落在 ad_daily 上;
    touchpoints / orders 只带 date_id + channel_id(+ campaign_id)。
  - 金额一律整数「分」;订单时间戳是 UTC、date_id 取 UTC 日 —— C3 的跨日错位来源。
  - 漏斗在数据里成立:曝光 → 点击(CTR)→ 转化(CVR)、消耗 = 点击 × 点击单价;两个
    **故意的例外**就是坑本身:C1 只缺陷 platform_conv,B6 曝光被掐而计费照跑。
  - 固定种子 SEED=42,完全可复现。每个坑的完整描述见 cases/*.yaml 与 expectations.yaml。

埋点一览(B1~B6 漏斗形变 / C1~C5 数据质量 / D1~D3 归因陷阱;细节见 cases/*.yaml):
  B1 素材按投放天数衰减;B2 人群包点击单价 +45%;B3 一周 CVR −40% 而 CTR 不变;B4 日预算封顶;
  B5 频次疲劳;B6 素材被拒(曝光断崖而消耗照跑);C1 平台回传缺 70%;C2 两套口径差 23%;
  C3 时区错日;C4 埋点整段缺失;C5 停投空白期;D1 大促后回落;D2 品牌词加预算无增量;D3 季节相关非因果。

用法:python scripts/generate_marketing.py → 写出 data/*.parquet 并打印实测锚点。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.datasets import resolve_dataset  # noqa: E402

SEED = 42
START, END = "2026-03-01", "2026-08-31"
DAYS = [d.strftime("%Y-%m-%d") for d in pd.date_range(START, END)]
WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
B1_CREATIVE, B1_LAUNCH, B1_SLOPE = "CR_TT_007", "2026-05-20", 0.011
B2_AUDIENCE, B2_LAUNCH = "AUD_XHS_02", "2026-04-22"
B3_CAMPAIGN, B3_FROM, B3_TO, B3_CVR = "CAMP_TM_02", "2026-03-16", "2026-03-22", 0.60
B4_CAMPAIGN, B4_FROM, B4_BUDGET = "CAMP_WX_02", "2026-04-01", 800_000
B5_AUDIENCE, B5_FROM = "AUD_DY_02", "2026-08-11"
B6_CREATIVE, B6_FROM, B6_CUT = "CR_XHS_004", "2026-08-05", 0.05
C1_FROM, C1_RATIO, C2_RATIO = "2026-08-25", 0.30, 1.23
C3_DAY, C3_NEXT, C3_HOUR, C3_EXTRA = "2026-04-21", "2026-04-22", 18, 340
C4_CHANNEL, C4_FROM, C4_TO = "CH_JD", "2026-05-04", "2026-05-06"
C5_CAMPAIGN, C5_FROM, C5_TO = "CAMP_JD_01", "2026-07-05", "2026-07-18"
D2_CAMPAIGN, D2_FROM, D2_LIFT, D2_CVR = "CAMP_TM_01", "2026-08-01", 1.60, 0.65
PROMO_FROM, PROMO_TO, PROMO_LIFT = "2026-06-01", "2026-06-18", 1.60
PROMO_TAIL_FROM, PROMO_TAIL_TO, PROMO_TAIL = "2026-06-19", "2026-06-30", 0.95
# (渠道 id, 渠道名, 渠道类型)
CHANNELS = [("CH_DY", "抖音", "短视频平台"), ("CH_XHS", "小红书", "种草社区"),
            ("CH_WX", "微信视频号", "短视频平台"), ("CH_TM", "天猫直通车", "电商站内"),
            ("CH_JD", "京东快车", "电商站内"), ("CH_PRIV", "私域", "自有触点"),
            ("CH_ORG", "自然流量", "免费流量")]
# (计划 id, 计划名, 渠道, 计划类型, 付费, 品牌词, 日预算分, 日曝光基数, 客单价分, CTR, 点击单价分, CVR, 频次)
CAMPAIGNS = [
    ("CAMP_DY_01", "抖音-效果通投A", "CH_DY", "信息流", "付费", "非品牌词", 3_000_000, 444_200, 24_000, 0.030, 150, 0.0120, 2.6),
    ("CAMP_DY_02", "抖音-人群包精准B", "CH_DY", "信息流", "付费", "非品牌词", 2_500_000, 383_400, 23_000, 0.028, 150, 0.0110, 2.4),
    ("CAMP_DY_03", "抖音-品牌专区", "CH_DY", "品牌词", "付费", "品牌词", 800_000, 54_700, 52_000, 0.075, 90, 0.0450, 1.8),
    ("CAMP_XHS_01", "小红书-种草通投", "CH_XHS", "内容种草", "付费", "非品牌词", 2_000_000, 293_300, 26_000, 0.026, 170, 0.0100, 2.2),
    ("CAMP_XHS_02", "小红书-搜索品牌词", "CH_XHS", "品牌词", "付费", "品牌词", 600_000, 23_000, 48_000, 0.070, 100, 0.0420, 1.7),
    ("CAMP_WX_01", "视频号-信息流", "CH_WX", "信息流", "付费", "非品牌词", 1_500_000, 229_800, 21_000, 0.022, 200, 0.0090, 2.0),
    ("CAMP_WX_02", "视频号-扩量计划", "CH_WX", "信息流", "付费", "非品牌词", B4_BUDGET, 246_000, 22_000, 0.024, 190, 0.0100, 2.1),
    ("CAMP_TM_01", "直通车-品牌词", "CH_TM", "品牌词", "付费", "品牌词", 1_200_000, 71_000, 55_000, 0.065, 110, 0.0400, 1.6),
    ("CAMP_TM_02", "直通车-类目词", "CH_TM", "类目词", "付费", "非品牌词", 1_600_000, 189_200, 28_000, 0.031, 140, 0.0130, 2.3),
    ("CAMP_JD_01", "京快-类目词", "CH_JD", "类目词", "付费", "非品牌词", 900_000, 154_200, 25_000, 0.029, 160, 0.0110, 2.2),
    ("CAMP_JD_02", "京快-品牌词", "CH_JD", "品牌词", "付费", "品牌词", 500_000, 10_300, 50_000, 0.072, 95, 0.0430, 1.5),
    ("CAMP_PRIV_01", "私域-企微运营", "CH_PRIV", "私域运营", "自然", "非品牌词", 0, 0, 18_000, 0, 0, 0, 0),
    ("CAMP_ORG_01", "自然搜索", "CH_ORG", "自然搜索", "自然", "非品牌词", 0, 0, 30_000, 0, 0, 0, 0),
]
# 非付费计划的日订单基数(没有广告行,只有业务订单 —— D2 的「自然流量」对照面)
ORGANIC_ORDERS = {"CAMP_PRIV_01": 95, "CAMP_ORG_01": 120}
# (素材 id, 素材名, 所属计划, 素材类型, 上线日, 计划内流量占比, 退役日空串=在投)
CREATIVES = [
    ("CR_TT_001", "抖音A-口播带货", "CAMP_DY_01", "短视频", "2026-03-01", 0.45, ""),
    ("CR_TT_007", "抖音B-竖版剧情", "CAMP_DY_01", "短视频", B1_LAUNCH, 0.55, "2026-07-31"),
    ("CR_TT_002", "抖音C-达人混剪", "CAMP_DY_02", "短视频", "2026-03-01", 1.00, ""),
    ("CR_TT_003", "抖音D-品牌片", "CAMP_DY_03", "短视频", "2026-03-01", 1.00, ""),
    ("CR_XHS_001", "小红书A-图文种草", "CAMP_XHS_01", "图文", "2026-03-01", 0.40, ""),
    ("CR_XHS_004", "小红书B-视频种草", "CAMP_XHS_01", "短视频", "2026-07-06", 0.60, ""),
    ("CR_XHS_002", "小红书C-品牌笔记", "CAMP_XHS_02", "图文", "2026-03-01", 1.00, ""),
    ("CR_WX_001", "视频号A-口播", "CAMP_WX_01", "短视频", "2026-03-01", 1.00, ""),
    ("CR_WX_002", "视频号B-场景剧", "CAMP_WX_02", "短视频", "2026-03-01", 1.00, ""),
    ("CR_TM_001", "直通车A-品牌卡位", "CAMP_TM_01", "搜索", "2026-03-01", 1.00, ""),
    ("CR_TM_002", "直通车B-类目长图", "CAMP_TM_02", "搜索", "2026-03-01", 1.00, ""),
    ("CR_JD_001", "京快A-类目卡位", "CAMP_JD_01", "搜索", "2026-03-01", 1.00, ""),
    ("CR_JD_002", "京快B-品牌词卡位", "CAMP_JD_02", "搜索", "2026-03-01", 1.00, ""),
]
# (人群包 id, 人群包名, 人群类型, 起始日, 绑定计划, 结束日空串=在投)
AUDIENCES = [
    ("AUD_DY_01", "抖音-泛兴趣人群包", "泛兴趣", "2026-03-01", "CAMP_DY_01", ""),
    ("AUD_DY_02", "抖音-高活人群包", "高活跃", "2026-03-01", "CAMP_DY_02", ""),
    ("AUD_DY_03", "抖音-品牌意向人群包", "品牌意向", "2026-03-01", "CAMP_DY_03", ""),
    ("AUD_XHS_01", "小红书-泛兴趣人群包", "泛兴趣", "2026-03-01", "CAMP_XHS_01", ""),
    ("AUD_XHS_02", "小红书-美妆兴趣人群包", "兴趣人群", B2_LAUNCH, "CAMP_XHS_01", "2026-05-31"),
    ("AUD_XHS_03", "小红书-品牌搜索人群包", "品牌意向", "2026-03-01", "CAMP_XHS_02", ""),
    ("AUD_WX_01", "视频号-泛人群包", "泛兴趣", "2026-03-01", "CAMP_WX_01", ""),
    ("AUD_WX_02", "视频号-相似人群包", "相似人群", "2026-03-01", "CAMP_WX_02", ""),
    ("AUD_TM_01", "直通车-品牌意向人群包", "品牌意向", "2026-03-01", "CAMP_TM_01", ""),
    ("AUD_TM_02", "直通车-类目兴趣人群包", "兴趣人群", "2026-03-01", "CAMP_TM_02", ""),
    ("AUD_JD_01", "京快-类目人群包", "兴趣人群", "2026-03-01", "CAMP_JD_01", ""),
    ("AUD_JD_02", "京快-品牌意向人群包", "品牌意向", "2026-03-01", "CAMP_JD_02", ""),
]


def _days_since(day: str, start: str) -> int:
    """第几天(「投放第 N 天」;B1 / B2 都按它计,不按日历)。"""
    return (date.fromisoformat(day) - date.fromisoformat(start)).days + 1


def _demand(day: str) -> float:
    """全局需求:线性增长(3→8 月 +10%)× 618 脉冲(06-01~06-18 冲高、06-19~06-30 回落)。"""
    growth = 1.0 + 0.10 * DAYS.index(day) / (len(DAYS) - 1)
    factor = (PROMO_LIFT if PROMO_FROM <= day <= PROMO_TO else PROMO_TAIL
              if PROMO_TAIL_FROM <= day <= PROMO_TAIL_TO else 1.0)
    return growth * factor


def _b5(day: str) -> float:
    """B5 进度 0→1(2026-08-11 起 14 天):投放量放大、频次抬升、CTR 反降。"""
    return 0.0 if day < B5_FROM else min(1.0, _days_since(day, "2026-08-10") / 14.0)


def _ctr_mult(creative: str, audience: str, day: str) -> float:
    """B1 按「投放天数」单调衰减(与日历日期无关);B5 因频次过高的 CTR 衰减。"""
    if creative == B1_CREATIVE:
        return 1.0 if (n := _days_since(day, B1_LAUNCH)) < 12 else 1.0 - B1_SLOPE * (n - 11)
    return 1.0 - 0.55 * _b5(day) if audience == B5_AUDIENCE else 1.0


def _impr_mult(campaign: str, audience: str, day: str) -> float:
    """曝光放大:D2 品牌词计划 2026-08 起预算 +60%;B5 人群包投放量抬升(频次同涨)。"""
    scale = D2_LIFT if campaign == D2_CAMPAIGN and day >= D2_FROM else 1.0
    return scale * (1.0 + 1.10 * _b5(day) if audience == B5_AUDIENCE else 1.0)


def _cpc_mult(audience: str, day: str) -> float:
    """B2:AUD_XHS_02 上线第 20 天起点击单价 +45%(点击量不变,消耗被单价推高)。"""
    if audience != B2_AUDIENCE:
        return 1.0
    return 1.0 if (n := _days_since(day, B2_LAUNCH)) < 20 else 1.0 + 0.45 * min(1.0, (n - 19) / 4.0)


def _cvr_mult(campaign: str, day: str) -> float:
    """B3:CAMP_TM_02 一周 CVR 掉 40%(落地页故障);D2:扩量后 CVR 被摊薄。"""
    if campaign == B3_CAMPAIGN and B3_FROM <= day <= B3_TO:
        return B3_CVR
    return D2_CVR if campaign == D2_CAMPAIGN and day >= D2_FROM else 1.0


def _creatives_of(campaign: str, day: str) -> list[tuple[str, float]]:
    """该计划当日**在投**素材及归一化占比(新素材上线后按占比与原素材分流)。"""
    live = [(cr, w) for cr, _, cp, _, launch, w, retire in CREATIVES
            if cp == campaign and launch <= day and (not retire or day <= retire)]
    total = sum(w for _, w in live) or 1.0
    return [(cr, w / total) for cr, w in live]


def _audience_of(campaign: str, day: str) -> str:
    """计划当日绑定的人群包(同时命中多个取最晚开始的;到期后回落泛兴趣包)。"""
    live = [a for a in AUDIENCES if a[4] == campaign and a[3] <= day and (not a[5] or day <= a[5])]
    return max(live, key=lambda a: a[3])[0] if live else ""


def _orders(day: str, channel: str, campaign: str, count: int, aov: int, rng,
            utc_hour: int | None = None) -> list[tuple]:
    """按北京时间生成订单并转 UTC;**date_id 取 UTC 日**(C3 的跨日错位在此发生)。"""
    if count <= 0:
        return []
    if utc_hour is None:
        hour = rng.choice([9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23], size=count,
                          p=[.06, .08, .08, .07, .05, .06, .05, .06, .07, .08, .09, .10, .09, .06])
        stamp = (pd.to_datetime(day) + pd.to_timedelta(hour, unit="h")
                 + pd.to_timedelta(rng.integers(0, 3600, count), unit="s") - pd.Timedelta(hours=8))
    else:   # C3:北京时间次日凌晨的跨夜专场,UTC 时间戳落在当日 utc_hour 点
        stamp = (pd.to_datetime(day) + pd.to_timedelta(utc_hour, unit="h") + pd.to_timedelta(
            rng.integers(0, 4 * 3600, count), unit="s"))
    amount = np.maximum(1000, aov * rng.lognormal(0.0, 0.38, count)).astype("int64")
    quantity = rng.integers(1, 4, count)
    return [(d.strftime("%Y-%m-%d"), channel, campaign, d.strftime("%Y-%m-%d %H:%M:%S"),
             int(amount[i]), int(quantity[i]))
            for i, d in enumerate(pd.Series(stamp).dt.to_pydatetime())]


def build() -> dict[str, pd.DataFrame]:
    """按「曝光 → 点击 → 转化 → 订单 / 消耗」的漏斗顺序生成 8 张表。"""
    rng = np.random.default_rng(SEED)
    ad_rows: list[tuple] = []
    order_rows: list[tuple] = []
    ch_orders: dict[tuple[str, str], int] = {}
    for day in DAYS:
        week = 1.10 if date.fromisoformat(day).weekday() >= 5 else 1.00
        dem = _demand(day)
        for cid, _, ch, _, paid, _, _, impr0, aov, ctr0, cpc0, cvr0, freq0 in CAMPAIGNS:
            if cid == C5_CAMPAIGN and C5_FROM <= day <= C5_TO:
                continue          # C5:停投期 ad_daily / orders 都没有行(不是 0)
            if paid == "自然":    # 非付费计划没有广告行,只有业务订单(私域 / 自然搜索)
                count = max(0, int(round(ORGANIC_ORDERS[cid] * week * dem * rng.normal(1, 0.07))))
                order_rows += _orders(day, ch, cid, count, aov, rng)
                ch_orders[(day, ch)] = ch_orders.get((day, ch), 0) + count
                continue
            plan = []             # (素材, 人群, 曝光, 点击, 消耗, 转化)
            for cr, share in _creatives_of(cid, day):
                aud = _audience_of(cid, day)
                impr = (impr0 * share * week * dem * _impr_mult(cid, aud, day)
                        * rng.normal(1.0, 0.05))
                clicks = impr * ctr0 * _ctr_mult(cr, aud, day) * rng.normal(1.0, 0.05)
                billed = clicks                       # B6:计费按「本该有的点击」走
                if cr == B6_CREATIVE and day >= B6_FROM:
                    impr, clicks = impr * B6_CUT, clicks * B6_CUT     # 曝光/点击断崖
                plan.append([cr, aud, impr, clicks, billed * cpc0 * _cpc_mult(aud, day),
                             clicks * cvr0 * _cvr_mult(cid, day)])
            if cid == B4_CAMPAIGN and day >= B4_FROM:
                # B4:日预算下调 → 当日消耗被顶在预算上(带平台抖动),量级等比压缩
                cap = B4_BUDGET * float(rng.normal(1.0, 0.006))
                raw = sum(p[4] for p in plan)
                scale = min(1.0, cap / raw) if raw > cap else 1.0
                for row in plan:
                    row[2], row[3], row[4], row[5] = (row[2] * scale, row[3] * scale,
                                                      row[4] * scale, row[5] * scale)
            for cr, aud, impr, clicks, cost, conv in plan:
                plat = conv * (C1_RATIO if day >= C1_FROM else 1.0)   # C1:只缺陷平台口径
                freq = freq0 * (1.0 + (_b5(day) if aud == B5_AUDIENCE else 0.0))
                ad_rows.append((day, cid, cr, aud, ch, int(round(cost)), int(round(impr)),
                                int(round(clicks)), int(round(impr / freq)), int(round(plat))))
                count = int(round(conv / C2_RATIO))
                order_rows += _orders(day, ch, cid, count, aov, rng)
                ch_orders[(day, ch)] = ch_orders.get((day, ch), 0) + count
        if day == C3_DAY:         # C3:跨夜专场的 340 单被 UTC 日切记成 04-21
            order_rows += _orders(day, "CH_PRIV", "CAMP_PRIV_01", C3_EXTRA, 18_000, rng,
                                  utc_hour=C3_HOUR)
            ch_orders[(day, "CH_PRIV")] = ch_orders.get((day, "CH_PRIV"), 0) + C3_EXTRA
    touch_rows = []
    for (day, ch), count in sorted(ch_orders.items()):
        if ch == C4_CHANNEL and C4_FROM <= day <= C4_TO:
            continue              # C4:埋点整段缺失(没有行,不是 0)
        if ch == "CH_PRIV" and day in (C3_DAY, C3_NEXT):
            # C3:埋点按北京时间记日,这笔流量不在 04-21 而在次日凌晨
            count += -C3_EXTRA if day == C3_DAY else C3_EXTRA
        sessions = count / 0.022 * float(rng.normal(1.0, 0.06))
        touch_rows.append((day, ch, int(round(sessions)), int(round(sessions * 3.2)),
                           int(round(sessions * 0.12))))
    order_rows.sort(key=lambda r: (r[0], r[2]))
    tables = {
        "ad_daily": (ad_rows, "date_id campaign_id creative_id audience_id channel_id cost impressions clicks reach platform_conv"),
        "touchpoints": (touch_rows, "date_id channel_id sessions page_views add_carts"),
        "orders": ([(r[0], f"OD{i + 1:08d}", *r[1:]) for i, r in enumerate(order_rows)], "date_id order_id channel_id campaign_id created_at gmv_amount quantity"),
        "dim_campaign": ([(c[0], c[1], c[2], c[3], c[4], c[5], c[6], "2026-03-01") for c in CAMPAIGNS], "campaign_id campaign_name channel_id campaign_type is_paid is_brand daily_budget launch_date"),
        "dim_creative": ([(c[0], c[1], c[2], c[3], c[4]) for c in CREATIVES], "creative_id creative_name campaign_id creative_type launch_date"),
        "dim_audience": ([(a[0], a[1], a[2], a[3]) for a in AUDIENCES], "audience_id audience_name audience_type start_date"),
        "dim_channel": (CHANNELS, "channel_id channel_name channel_type"),
        "dim_date": ([(d, d, int(d[:4]), int(d[5:7]), int(d[8:10]), WEEKDAY_CN[date.fromisoformat(d).weekday()]) for d in DAYS], "date_id date year month day day_of_week"),
    }
    return {name: pd.DataFrame(rows, columns=spec.split())
            for name, (rows, spec) in tables.items()}


def main() -> None:
    """写出全部 parquet 到数据集包的 data/ 目录,并打印实测锚点(供 cases / tests 引用)。"""
    out = Path(resolve_dataset("marketing-funnel", datasets_dir=ROOT / "datasets").data_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, frame in build().items():
        frame.to_parquet(out / f"{name}.parquet", index=False)
        print(f"写出 {name}.parquet:{len(frame)} 行")
    ad = f"read_parquet('{(out / 'ad_daily.parquet').as_posix()}')"
    od = f"read_parquet('{(out / 'orders.parquet').as_posix()}')"
    tp = f"read_parquet('{(out / 'touchpoints.parquet').as_posix()}')"
    def win(col: str, start: str, end: str) -> str:
        """窗口内该列的合计(拼进聚合 SQL 的 CASE 表达式)。"""
        return f"sum(CASE WHEN date_id BETWEEN '{start}' AND '{end}' THEN {col} END)"

    def show(label: str, expr: str, table: str, where: str = "1=1") -> None:
        """跑一条锚点 SQL 并打印结果。"""
        print(f"  {label}: {duckdb.sql(f'SELECT {expr} FROM {table} WHERE {where}').fetchall()}")

    paid_orders = (f"(SELECT count(*) FROM {od} o JOIN "
                   f"read_parquet('{(out / 'dim_campaign.parquet').as_posix()}') c"
                   f" USING (campaign_id) WHERE c.is_paid='付费' AND o.date_id LIKE '2026-07%')")
    plat_july = f"(SELECT {win('platform_conv', '2026-07-01', '2026-07-31')} FROM {ad})"
    print("\n== 实测锚点(cases / expectations / tests 引用)==")
    show("B1 CTR% 06-20~07-04/07-15~07-29", f"round({win('clicks*100.', '2026-06-20', '2026-07-04')}/{win('impressions', '2026-06-20', '2026-07-04')},3), round({win('clicks*100.', '2026-07-15', '2026-07-29')}/{win('impressions', '2026-07-15', '2026-07-29')},3)", ad, f"creative_id='{B1_CREATIVE}'")
    show("B5 频次 08-01~10/08-15~24 与 CTR% 同期", f"round({win('impressions*1.', '2026-08-01', '2026-08-10')}/{win('reach', '2026-08-01', '2026-08-10')},2), round({win('impressions*1.', '2026-08-15', '2026-08-24')}/{win('reach', '2026-08-15', '2026-08-24')},2), round({win('clicks*100.', '2026-08-01', '2026-08-10')}/{win('impressions', '2026-08-01', '2026-08-10')},3), round({win('clicks*100.', '2026-08-15', '2026-08-24')}/{win('impressions', '2026-08-15', '2026-08-24')},3)", ad, f"audience_id='{B5_AUDIENCE}'")
    show("B6 曝光 07-06~25/08-05~24 与 消耗分 同期", f"{win('impressions', '2026-07-06', '2026-07-25')},{win('impressions', '2026-08-05', '2026-08-24')}, {win('cost', '2026-07-06', '2026-07-25')},{win('cost', '2026-08-05', '2026-08-24')}", ad, f"creative_id='{B6_CREATIVE}'")
    show("C1 平台转化与转化率% 08-18~24/08-25~31", f"{win('platform_conv', '2026-08-18', '2026-08-24')},{win('platform_conv', '2026-08-25', '2026-08-31')}, round({win('platform_conv*100.', '2026-08-18', '2026-08-24')}/{win('clicks', '2026-08-18', '2026-08-24')},3), round({win('platform_conv*100.', '2026-08-25', '2026-08-31')}/{win('clicks', '2026-08-25', '2026-08-31')},3)", ad)
    show("C2 平台口径转化 / 付费业务订单 2026-07 与比值", f"{plat_july}, {paid_orders}, round({plat_july}*1./{paid_orders},4)", ad, "date_id='2026-07-01' LIMIT 1")
    show("C3 CAMP_PRIV_01 订单 04-21/04-22 与埋点会话同两日", f"(SELECT count(*) FROM {od} WHERE campaign_id='CAMP_PRIV_01' AND date_id='2026-04-21'), (SELECT count(*) FROM {od} WHERE campaign_id='CAMP_PRIV_01' AND date_id='2026-04-22'), (SELECT round(sum(sessions)) FROM {tp} WHERE channel_id='CH_PRIV' AND date_id='2026-04-21'), (SELECT round(sum(sessions)) FROM {tp} WHERE channel_id='CH_PRIV' AND date_id='2026-04-22')", ad, "date_id='2026-04-21' LIMIT 1")
    show("D2 CAMP_TM_01 消耗分/转化/CPA 07-01~24 与 08-01~24", f"{win('cost', '2026-07-01', '2026-07-24')},{win('cost', '2026-08-01', '2026-08-24')}, {win('platform_conv', '2026-07-01', '2026-07-24')},{win('platform_conv', '2026-08-01', '2026-08-24')}, round({win('cost*1.', '2026-07-01', '2026-07-24')}/{win('platform_conv', '2026-07-01', '2026-07-24')},1), round({win('cost*1.', '2026-08-01', '2026-08-24')}/{win('platform_conv', '2026-08-01', '2026-08-24')},1)", ad, f"campaign_id='{D2_CAMPAIGN}'")
    show("C4 CH_JD 埋点会话 05-01~03/05-04~06/05-07~09", f"{win('sessions', '2026-05-01', '2026-05-03')},{win('sessions', '2026-05-04', '2026-05-06')}, {win('sessions', '2026-05-07', '2026-05-09')}", tp, f"channel_id='{C4_CHANNEL}'")


if __name__ == "__main__":
    main()
