# -*- coding: utf-8 -*-
"""生成 user-journey 数据集(用户旅程:事件流 + 同期群;M5 四套数据集里的第三套)。

设计要点(docs/REUSE_DESIGN.md §5.5 坑表 / §5.10 隔离与可回验):
  - 2025-06-01 ~ 2026-06-30(395 天)。**一行 = 一个事件**(访问 / 注册 / 付费);用户属性
    (渠道 / 分层 / 版本 / 注册日)与回填日**冗余在事实行上** —— §5.5 指定的形态:同期群是
    **派生维度**(字段住在事实表上,引擎不 join)。
  - 用户盘必须**处在均衡态**:拉新恒定(800/月)+ DAY0 按稳态年龄结构铺好存量。否则窗口内
    用户盘还在「年轻化」,聚合人均会带上一截**与业务无关的上行漂移**(实测曾达 +7%/月),
    U4 的结构分解会把它记成「自身效应」,把 mix 效应从 100% 压到 60%。
  - 核心考法:**跨粒度的指标** —— 访问次数可加、活跃用户跨时间不可加、DAU 半可加、注册转化率
    是跨粒度比率(分子分母人群不同)、人均收入是「分子 / 分母」形派生指标:同一个「有多少
    用户」在四种口径下是四个不同的数。
  - 铁律:①切片 × 时间隔离(六组埋点各占不同的维度 × 时间窗,受影响集合两两不交);②回填可反查
    (记录注册日晚于最早事件日、入库日 ≠ 事件日 —— 不读 case 也能反着测出来);③种子 SEED=42。
    六组埋点逐个说明见 datasets/user-journey/cases/*.yaml —— 改常量即改答案,须同步 tests。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))       # 直接执行脚本时也能 import harness(重复插入无害)

from harness.datasets import resolve_dataset  # noqa: E402

DATA_DIR = resolve_dataset("user-journey", datasets_dir=ROOT / "datasets").data_dir
DATA_DIR.mkdir(exist_ok=True, parents=True)

SEED = 42
rng = np.random.default_rng(SEED)

DAY0, DAY1 = date(2025, 6, 1), date(2026, 6, 30)   # 13 个月:留足时间让用户盘先到均衡
NDAYS = (DAY1 - DAY0).days + 1
DAYS = [DAY0 + timedelta(days=i) for i in range(NDAYS)]

# 拉新节奏(人/月):**恒定**。拉新一变,用户盘的年龄结构与活跃率就跟着漂(年龄结构 = 拉新史
# × 留存曲线),聚合人均会带上一截自己造出来的伪信号。均衡态 ≈ 800 × E[寿命]164/30 ≈ 4,300 人。
ARRIVALS = 800
MONTHS = [(2025, m) for m in range(6, 13)] + [(2026, m) for m in range(1, 7)]
CHURN_AFTER, CHURN_P, LOYAL = 60, 0.25, 0.05   # 注册 60 天后按月 25% 退出;5% 的忠实用户不退出
PRIME_MAX_AGE = 400         # 存量用户最老 400 天(再老的人数权重 <4%,对聚合无影响)

# (渠道 ID, 名称, 类型, 拉新占比)
CHANNELS = (("CH_APPSTORE", "应用商店", "应用商店", 0.30),
            ("CH_SOCIAL", "社交裂变", "社交", 0.18),
            ("CH_ADS", "信息流广告", "付费投放", 0.24),
            ("CH_OFFLINE", "线下地推", "线下", 0.12),
            ("CH_ORGANIC", "自然口碑", "自然", 0.16))
CH_IDS = [c[0] for c in CHANNELS]

# (分层 ID, 活跃倍率, 单日付费概率, 单笔金额下界, 上界;单位:分)
# 付费「高频小额」是刻意的:每个分层的**自身人均**要稳定到 ±5% 以内,否则 u4 的结构分解
# 会被抽样噪声污染(金额区间越宽,自身效应越像真的)。窄区间 + 高概率 = CV 降到 ~2%。
TIERS = (("高价值", 1.60, 0.55, 800, 2200),
         ("中价值", 1.00, 0.35, 400, 1200),
         ("低价值", 0.55, 0.18, 150, 450))
TIER_IDS = [t[0] for t in TIERS]

# (App 版本, 占比)。V4.2.0 是新版本、占比小:它的崩塌只吃掉大盘一小口。
VERSIONS = (("V4.0.8", 0.26), ("V4.1.3", 0.30), ("V4.2.0", 0.06), ("V4.2.1", 0.38))
VER_IDS = [v[0] for v in VERSIONS]

# ---- 六组埋点的常量(改这里 = 改答案,须同步 cases / expectations / tests)----
U1_CHANNEL, U1_MONTH = "CH_SOCIAL", "2026-01"
U1_CLIFF, U1_RESIDUAL = date(2026, 5, 1), 0.18
U2_CHANNEL, U2_MONTH, U2_NEW_FACTOR = "CH_ADS", "2026-03", 0.33
U3_VERSION, U3_CLIFF, U3_RESIDUAL = "V4.2.0", date(2026, 4, 8), 0.02
U4_TIER, U4_CLIFF, U4_CHURN_SHARE = "低价值", date(2026, 6, 1), 0.35
U5_WEEKS = (("2026-06-01", "2026-06-07"), ("2026-06-08", "2026-06-14"))
U6_CHANNEL, U6_TRUE = "CH_OFFLINE", (date(2026, 1, 5), date(2026, 1, 18))
U6_BACKFILL, U6_BATCH = date(2026, 2, 5), 150

# 日活跃概率曲线 p(注册后天数):新用户高、长尾低(留存曲线的形状)
CURVE = 0.30 * np.exp(-np.arange(NDAYS + PRIME_MAX_AGE + 50) / 45.0) + 0.030
# 单日系数(仅 2026-06):u5 的两种口径靠这三处**反向**错开 —— 06-03 的单日冲高把
# **逐日求和**的 W1 抬高,而 06-13/14 的月末回暖让**末日口径**的 W2 反而更高。
DAY_FACTOR = {date(2026, 6, 3): 1.35, date(2026, 6, 13): 1.08, date(2026, 6, 14): 1.12}


def _idx(when: date) -> int:
    return (when - DAY0).days


def _month_of(when: date) -> str:
    return when.strftime("%Y-%m")


def _build_users() -> dict[str, np.ndarray]:
    """造用户:签入日 / 渠道 / 分层 / 版本 / 活跃度 / 自然流失日 / 实时与回填的注册日。

    两遍:**先铺 DAY0 的存量**(年龄按稳态分布抽,公式见下),再逐月造窗口期的新增。
    **u6 的地推批次单独造**(渠道锁死 CH_OFFLINE、真实签入日散在 1 月上旬),其余按各渠道的
    拉新占比抽(唯一例外是 u2 的当月:该渠道的占比被掐到 33%)。
    """
    signup, channel, tier, version, engage, churn, u6, guard = [], [], [], [], [], [], [], []
    ch_w = np.array([c[3] for c in CHANNELS])
    ch_w = ch_w / ch_w.sum()
    tier_w = np.array([0.25, 0.45, 0.30])
    ver_w = np.array([v[1] for v in VERSIONS])
    ver_w = ver_w / ver_w.sum()
    ver_w_safe = ver_w.copy()
    ver_w_safe[VER_IDS.index(U3_VERSION)] = 0.0     # 受保护的用户不落在 u3 的版本上
    ver_w_safe = ver_w_safe / ver_w_safe.sum()

    def add(day_idx: int, forced_channel: int | None, month_weights: np.ndarray,
            is_u6: bool, prime_age: int | None = None) -> None:
        signup.append(day_idx)
        ch = (int(rng.choice(len(CHANNELS), p=month_weights))
              if forced_channel is None else forced_channel)
        channel.append(ch)
        # 隔离:u1 批次 / u2 同期群 / u6 批次三类用户不落在 u3 的版本上,也不进 u4 的流失集。
        # 否则 u1 的「批次崩塌」里混着 u3 的版本崩塌,u4 的「低价值层结构效应」里混着别人的流失。
        month = _month_of(DAY0 + timedelta(days=day_idx))
        protected = (is_u6 or (CH_IDS[ch] == U1_CHANNEL and month == U1_MONTH)
                     or (CH_IDS[ch] == U2_CHANNEL and month == U2_MONTH))
        guard.append(protected)
        tier.append(int(rng.choice(len(TIERS), p=tier_w)))
        version.append(int(rng.choice(len(VERSIONS), p=ver_w_safe if protected else ver_w)))
        engage.append(float(np.clip(rng.lognormal(0.0, 0.55), 0.15, 6.0)))
        # 流失日 = 注册 + 60 + 30K(K ~ 几何)。存量用户条件在「活到 prime_age」上(跨过
        # prime_age 之后的检查点再按几何无记忆重抽),否则他们会在 DAY0 前成批死掉,
        # 存量里恰好缺掉最活跃的一截,聚合人均又会漂。
        gap = 0 if prime_age is None else max(0, (prime_age - CHURN_AFTER) // 30 + 1)
        rested = CHURN_AFTER + 30 * gap
        churn.append(day_idx + rested + 30 * int(rng.geometric(CHURN_P))
                     if rng.random() >= LOYAL else NDAYS)
        u6.append(is_u6)

    # DAY0 存量:年龄 a 的人数 = 拉新率 × 存活率(a ≤60 天必活,之后每月 ×0.75)
    for age in range(1, PRIME_MAX_AGE + 1):
        survival = 1.0 if age <= CHURN_AFTER else 0.75 ** ((age - CHURN_AFTER) / 30.0)
        for _ in range(int(rng.poisson(ARRIVALS / 30.0 * survival))):
            add(-age, None, ch_w, False, prime_age=age)
    for year, mon in MONTHS:
        month, count = f"{year}-{mon:02d}", ARRIVALS
        first = date(year, mon, 1)
        last = min(DAY1, date(year + (mon == 12), mon % 12 + 1, 1) - timedelta(days=1))
        span = (last - first).days + 1
        weights = ch_w.copy()
        if month == U2_MONTH:                      # u2:该渠道当月拉新被掐(访问量不受影响)
            weights[CH_IDS.index(U2_CHANNEL)] *= U2_NEW_FACTOR
            weights = weights / weights.sum()
        for offset, number in enumerate(rng.multinomial(count, np.ones(span) / span)):
            for _ in range(int(number)):
                add(_idx(first + timedelta(days=offset)), None, weights, False)
    # u6 批次(单独一遍,渠道锁死):真实首活散在 1 月上旬,记录注册日 = 批次上传日
    u6_span = (U6_TRUE[1] - U6_TRUE[0]).days + 1
    for i in range(U6_BATCH):
        add(_idx(U6_TRUE[0]) + i % u6_span, CH_IDS.index(U6_CHANNEL), ch_w, True)
    signup_arr = np.array(signup)
    data = {"signup": signup_arr, "channel": np.array(channel), "tier": np.array(tier),
            "version": np.array(version), "engage": np.array(engage),
            "churn": np.array(churn), "u6": np.array(u6), "guard": np.array(guard)}
    month = np.array([_month_of(DAY0 + timedelta(days=int(i))) for i in signup_arr])
    # 三组埋点的标记(规则化:批次 = 渠道 × 注册月,不点名任何具体用户)
    data["u1_batch"] = ((data["channel"] == CH_IDS.index(U1_CHANNEL)) & (month == U1_MONTH))
    data["u3_version"] = data["version"] == VER_IDS.index(U3_VERSION)
    data["u4_churn"] = ((data["tier"] == TIER_IDS.index(U4_TIER))
                        & (rng.random(len(signup)) < U4_CHURN_SHARE)
                        & (data["churn"] > _idx(U4_CLIFF)) & ~data["u3_version"] & ~data["guard"])
    data["uid"] = np.array([f"U{i:06d}" for i in range(1, len(signup) + 1)])
    # 回填:这批人的「注册日」= 批次上传日(真实首活仍在 1 月上旬)
    signup_iso = np.array([(DAY0 + timedelta(days=int(i))).isoformat() for i in signup_arr])
    recorded = np.where(data["u6"], U6_BACKFILL.isoformat(), signup_iso)
    data["recorded"] = recorded
    data["recorded_month"] = np.array([d[:7] for d in recorded])
    data["channel_id"] = np.array(CH_IDS)[data["channel"]]
    data["tier_id"] = np.array(TIER_IDS)[data["tier"]]
    data["app_version"] = np.array(VER_IDS)[data["version"]]
    data["batch_id"] = np.array([f"{c}-{m}" for c, m in zip(data["channel_id"],
                                                            data["recorded_month"])])
    return data


def _activity_probability(users: dict, day: int) -> np.ndarray:
    """某天的逐用户活跃概率(留存曲线 × 个人活跃度 × 分层倍率 × 四处埋点倍率 × 单日系数)。"""
    age = np.clip(day - users["signup"], 0, len(CURVE) - 1)
    prob = (CURVE[age] * users["engage"]
            * np.array([t[1] for t in TIERS])[users["tier"]])
    factor = (np.where(users["u1_batch"] & (day >= _idx(U1_CLIFF)), U1_RESIDUAL, 1.0)
              * np.where(users["u3_version"] & (day >= _idx(U3_CLIFF)), U3_RESIDUAL, 1.0)
              * np.where(users["u4_churn"] & (day >= _idx(U4_CLIFF)), 0.01, 1.0))
    prob = np.clip(prob * factor * DAY_FACTOR.get(DAY0 + timedelta(days=day), 1.0), 0, 1)
    return np.where(age == 0, 1.0, prob)     # 注册当天必活跃:每个拉来的人都会留下注册事件


def _build_events(users: dict) -> dict[str, np.ndarray]:
    """逐日抽活跃、落事件行:每次活跃 1 条访问,按分层概率追加付费,注册当天追加注册。"""
    ev_day, ev_user, ev_type, ev_amount = [], [], [], []
    pay_p = np.array([t[2] for t in TIERS])
    pay_lo = np.array([t[3] for t in TIERS])
    pay_hi = np.array([t[4] for t in TIERS])
    for day in range(NDAYS):
        alive = (users["churn"] > day) & (users["signup"] <= day)
        active = np.flatnonzero(alive & (rng.random(len(alive))
                                         < _activity_probability(users, day)))
        stamp = DAYS[day].isoformat()
        paying = active[rng.random(len(active)) < pay_p[users["tier"][active]]]
        fresh = active[users["signup"][active] == day]
        pay_amt = rng.integers(pay_lo[users["tier"][paying]],
                               pay_hi[users["tier"][paying]] + 1).astype(np.int64)
        for flags, kind, amount in ((active, "app_open", np.zeros(len(active), dtype=np.int64)),
                                    (fresh, "signup", np.zeros(len(fresh), dtype=np.int64)),
                                    (paying, "pay", pay_amt)):
            ev_day.append(np.full(len(flags), stamp, dtype=object))
            ev_user.append(flags)
            ev_type.append(np.full(len(flags), kind, dtype=object))
            ev_amount.append(amount)
    order = np.argsort(np.concatenate(ev_day), kind="stable")   # 按日期排序(稳定)
    return {"user": np.concatenate(ev_user)[order], "day": np.concatenate(ev_day)[order],
            "type": np.concatenate(ev_type)[order], "amount": np.concatenate(ev_amount)[order]}


def _write_tables(users: dict, events: dict[str, np.ndarray]) -> None:
    """事实表 + 三张维度表;事实行冗余携带全部用户属性(派生维度与回填痕迹都在这里)。"""
    uidx = events["user"]
    events_frame = pd.DataFrame({
        "day": events["day"], "user_id": users["uid"][uidx], "event_type": events["type"],
        "channel_id": users["channel_id"][uidx], "batch_id": users["batch_id"][uidx],
        "tier_id": users["tier_id"][uidx], "app_version": users["app_version"][uidx],
        "signup_date": users["recorded"][uidx], "signup_month": users["recorded_month"][uidx],
        # 入库日:正常事件 = 事件当天;u6 那批 1 月上旬的事件是 2026-02-05 一次性补采的
        "ingest_day": np.where(users["u6"][uidx] & (events["day"] < U6_BACKFILL.isoformat()),
                               U6_BACKFILL.isoformat(), events["day"]),
        "amount": events["amount"]})
    events_frame.to_parquet(DATA_DIR / "events.parquet", index=False)

    pd.DataFrame({
        "user_id": users["uid"], "signup_date": users["recorded"],
        "signup_month": users["recorded_month"], "channel_id": users["channel_id"],
        "tier_id": users["tier_id"], "app_version": users["app_version"],
        "ingest_day": users["recorded"]}).to_parquet(DATA_DIR / "dim_user.parquet", index=False)
    pd.DataFrame([c[:3] for c in CHANNELS], columns=["channel_id", "channel_name",
                 "channel_type"]).to_parquet(DATA_DIR / "dim_channel.parquet", index=False)
    pd.DataFrame([(d.isoformat(), d.strftime("%Y-%m"), d.year) for d in DAYS],
                 columns=["day", "month", "year"]
                 ).to_parquet(DATA_DIR / "dim_date.parquet", index=False)
    print(f"生成完毕: 用户 {len(users['uid'])} 户 / 事件 {len(events['day'])} 行 → {DATA_DIR}")


def main() -> None:
    users = _build_users()
    _write_tables(users, _build_events(users))


if __name__ == "__main__":
    main()

    import duckdb  # noqa: E402  末尾自检:只依赖 duckdb,直接读刚写出的 parquet

    con = duckdb.connect()
    con.execute("CREATE VIEW ev AS SELECT * FROM read_parquet("
                f"'{(DATA_DIR / 'events.parquet').as_posix()}')")
    q = con.execute
    one = lambda s: q(s).fetchone()[0]                                    # noqa: E731
    # [lo, end) 里两段窗口的「活跃用户」:两段式 CASE WHEN,比窗口函数短
    d1 = lambda lo, hi, end, col: (                                       # noqa: E731
        f"SELECT {col}, COUNT(DISTINCT CASE WHEN day <'{hi}' THEN user_id END),"
        f" COUNT(DISTINCT CASE WHEN day >='{hi}' THEN user_id END) FROM ev"
        f" WHERE day>='{lo}' AND day<'{end}' GROUP BY 1 ORDER BY 1")

    print("\n============ 内置验证 ============")
    print("事件行 / 用户数:", one("SELECT COUNT(*) FROM ev"),
          one("SELECT COUNT(DISTINCT user_id) FROM ev"))
    print("[① 大盘] 月: 活跃用户 / 收入(元) / 人均(元)")
    for r in q("SELECT substr(day,1,7), COUNT(DISTINCT user_id), SUM(amount)/100.0"
               " FROM ev GROUP BY 1 ORDER BY 1").fetchall():
        print(f"   {r[0]}: {r[1]:,} 人 / {r[2]:,.0f} 元 / 人均 {r[2]/r[1]:.2f} 元")
    print("[② u1] 4→5 月 渠道:", q(d1("2026-04-01", "2026-05-01", "2026-06-01",
                                       "channel_id")).fetchall())
    print("         社交批次的 4→5 月:", [r for r in q(
        d1("2026-04-01", "2026-05-01", "2026-06-01", "batch_id")).fetchall()
        if r[0].startswith("CH_SOCIAL")])
    print("[③ u2] CH_ADS 逐月 访问/注册:", q(
        "SELECT substr(day,1,7), SUM(event_type='app_open'), SUM(event_type='signup')"
        " FROM ev WHERE channel_id='CH_ADS' GROUP BY 1 ORDER BY 1").fetchall())
    print("[④ u3] 版本 3/1~4/7 → 4/8~4/30:",
          q(d1("2026-03-01", "2026-04-08", "2026-05-01", "app_version")).fetchall())
    print("[⑤ u4] 分层 5→6 月 用户 / 收入(元):", q(
        "SELECT tier_id, COUNT(DISTINCT CASE WHEN day<'2026-06-01' THEN user_id END),"
        " COUNT(DISTINCT CASE WHEN day>='2026-06-01' THEN user_id END),"
        " SUM(CASE WHEN day<'2026-06-01' THEN amount ELSE 0 END)/100.0,"
        " SUM(CASE WHEN day>='2026-06-01' THEN amount ELSE 0 END)/100.0"
        " FROM ev WHERE day>='2026-05-01' GROUP BY 1 ORDER BY 1").fetchall())
    print("[⑥ u5] 6/1~6/14 逐日 DAU:", q("SELECT day, COUNT(DISTINCT user_id) FROM ev"
          " WHERE day BETWEEN '2026-06-01' AND '2026-06-14' GROUP BY 1 ORDER BY 1").fetchall())
    print("         逐日求和(前 7 日 / 后 7 日):", q(
        "SELECT day<'2026-06-08', COUNT(*) FROM (SELECT DISTINCT day, user_id FROM ev"
        " WHERE day BETWEEN '2026-06-01' AND '2026-06-14') GROUP BY 1").fetchall())
    print("[⑦ u6] 记录注册日 > 最早事件日的用户:", q(
        "SELECT channel_id, COUNT(DISTINCT user_id) FROM (SELECT user_id, channel_id, day,"
        " signup_date, MIN(day) OVER (PARTITION BY user_id) fd FROM ev) t"
        " WHERE signup_date > fd GROUP BY 1").fetchall(),
        "| 入库日 ≠ 事件日:", one("SELECT COUNT(DISTINCT user_id) FROM ev WHERE ingest_day <> day"))
    print("============ 内置验证结束 ============")
    con.close()
