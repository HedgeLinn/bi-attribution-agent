# -*- coding: utf-8 -*-
"""生成 saas-mrr 数据集(订阅制 SaaS 的 MRR 归因;M5 四套数据集里的第一套)。

设计要点(docs/REUSE_DESIGN.md §5.6 半可加 / §5.10 隔离与可回验):
  - 时间跨度 2025-07-01 ~ 2026-06-30(12 个月)。**数据起点即水平零点**:所有账户都在
    2025-07-01 或之后签约,故 2025-06-30 的全量水平 = 0(加法分解的恒等式只在同起点
    的两段窗口上成立,起点必须是这里)。
  - 两张事实表:
      subscriptions(默认事实表):期快照,一账户一天一行,金额 = 该账户截至当日的累计变动
        (整数「分」)。账户流失后不再出行 —— 没有行 = 水平 0,不是缺失值。
      mrr_movements:变动明细,四类 movement_type + 带符号金额(降配 / 流失为负)。
  - 铁律一(变动 ↔ 快照严格对账):快照的每一天都由「该账户当日及以前的全部变动」逐笔
    相加得到 —— 两边是同一批整数,对账是构造性精确,不依赖浮点容差。
  - 铁律二(切片 × 时间隔离):四组埋点各占「不同套餐 × 不同月份」,受影响账户集合两两不交。
  - 铁律三:固定种子 SEED=42,完全可复现。

埋点(常量;cases / expectations / tests 与脚本末尾自检都引用这些数字):
  * s1 流失主导:旗舰版 ACC_F001(37 席 @¥999)2026-06-15 流失 → 6 月水平 -¥36,963。
  * s2 扩张塌陷:专业版 ACC_P001 3 月 8 日一次性扩容 +118 席(+¥35,282),4 月无扩容
      → 扩张流入从 3 月的 +¥35,282 掉到 4 月的 ~0;4 月水平因此走平(增长熄火)。
  * s3 跨期求和陷阱:基础版 ACC_B001(500 席 @¥99)2026-05-28 降配 -450 席(-¥44,550);
      同时埋一批「试用潮」账户(36 户 × 1 席,4 月初签约、**5 月中旬到期流失**)——
      它是「MRR 跨期求和」这条静默错误的显形摆锤,两种口径给出**不同的主因**:
        - **末日水平口径**(正确):基期取 4 月末的 299 元/户、对比期取到期前最后一天的
          299 元/户 → 切片变化 0,进不了排名;它们的流失是真实水平事件(合计 -¥10,764,
          占 5 月降幅 18.8%),但**逐切片看不出来**。
        - **逐日求和口径**(错误):4 月整月都在(求和 ¥301,990)、5 月只剩半个月
          (求和 ¥106,743)→ 求和后「掉」¥195,247,比真实主因(ACC_B001 求和口径
          -¥128,700)还大,于是把试用客户到期误判成主因。
  * s4 双因素并存:企业版 ACC_E001(24 席 @¥1999)2026-02-20 流失(-¥47,976);同时
      2 月企业版**零新签**(1 月还有 2 户)→ 流失与新签放缓并存,主因是流失。
"""
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import count
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))       # 直接执行脚本时也能 import harness(重复插入无害)

from harness.datasets import resolve_dataset  # noqa: E402

DATA_DIR = resolve_dataset("saas-mrr", datasets_dir=ROOT / "datasets").data_dir
DATA_DIR.mkdir(exist_ok=True, parents=True)

SEED = 42
rng = np.random.default_rng(SEED)

DAY0 = date(2025, 7, 1)          # 数据起点(水平零点)
DAY1 = date(2026, 6, 30)         # 数据终点

# 席位单价(整数「分」/席/月)。金额一律用整数分:对账与分解恒等式不碰浮点误差。
PRICES = {"基础版": 9900, "专业版": 29900, "旗舰版": 99900,
          "企业版": 199900, "试用版": 29900}
CODES = {"基础版": "B", "专业版": "P", "旗舰版": "F", "企业版": "E", "试用版": "T"}

KIND_NEW, KIND_EXP, KIND_CON, KIND_CHURN = "new_biz", "expansion", "contraction", "churn"

# 背景账户:(套餐, 户数, 席位数下界, 上界, 月流失概率, 签约日偏移天;None = 随机铺开)
# 企业版给固定签约日:1 月签 2 户、2 月零新签(s4 的「新签放缓」)。
BACKGROUND = (
    ("基础版", 12, 3, 17, 0.015, None),
    ("专业版", 10, 2, 13, 0.015, None),
    ("旗舰版", 5, 2, 11, 0.010, None),
    ("企业版", 3, 2, 9, 0.010, (96, 200, 210)),
    ("试用版", 10, 1, 5, 0.250, None),
)
# 试用潮(s3 的「跨期求和」摆锤):4 月初签约、**5 月中旬**到期流失;死在 5 月中旬是
# 刻意选择——它对「末日水平」口径完全不可见,却让「逐日求和」口径炸掉半个 5 月。
BURST_COUNT, BURST_SEATS = 36, 1
BURST_START, BURST_END = date(2026, 4, 1), date(2026, 5, 8)

CITY = ("杭州", "苏州", "成都", "武汉", "西安", "南京", "长沙", "青岛")
WORD = ("云图", "数联", "格物", "星芒", "远澜", "致远", "鼎新", "旷世", "几何", "微光")
SUFFIX = ("科技有限公司", "信息技术有限公司", "数据服务有限公司", "网络科技有限公司")

_NAMES = count(1)   # 账户名序号(创建顺序确定 -> 名字唯一且可复现)


def _add_months(when: date, months: int) -> date:
    """月份偏移,返回当月 1 号(只用于「每月一次」的随机事件排期)。"""
    total = (when.year * 12 + when.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


@dataclass
class Account:
    """一个订阅账户:基础属性 + 全部变动事件(事件是两张事实表的唯一来源)。"""

    account_id: str
    account_name: str
    plan: str
    signup: date
    seats: int
    events: list[tuple[date, str, int]] = field(default_factory=list)

    def level(self, when: date | None = None) -> int:
        """截至 when(含)的累计变动 = 该日的 MRR 水平(整数分)。"""
        return sum(amount for day, _, amount in self.events if when is None or day <= when)

    def move(self, when: date, kind: str, delta_seats: int) -> None:
        """记一笔席位变动(扩容为正、降配为负);金额 = 席位数 × 席位单价。"""
        self.events.append((when, kind, delta_seats * PRICES[self.plan]))
        self.seats += delta_seats

    def churn(self, when: date) -> None:
        """流失:金额 = 届时全部剩余水平的相反数(与快照严格互补,账户归零)。"""
        self.events.append((when, KIND_CHURN, -self.level(when)))
        self.seats = 0


def _new_account(account_id: str, plan: str, signup: date, seats: int) -> Account:
    """新签账户:签约首笔变动 = 首月水平,席位单价取自套餐。"""
    index = next(_NAMES)
    account = Account(account_id, CITY[index % 8] + WORD[index % 10] + SUFFIX[index % 4],
                      plan, signup, seats)
    account.events.append((signup, KIND_NEW, seats * PRICES[plan]))
    return account


def _background_accounts() -> list[Account]:
    """背景账户:随机签约日 + 每月小额席位变动 + 低概率自然流失(噪声,不成规模)。"""
    accounts = []
    for plan, number, lo, hi, churn_p, fixed_days in BACKGROUND:
        for i in range(number):
            offset = fixed_days[i] if fixed_days else int(rng.integers(0, 300))
            signup = DAY0 + timedelta(days=offset)
            account = _new_account(f"ACC_{CODES[plan]}{i + 101:03d}", plan, signup,
                                   int(rng.integers(lo, hi + 1)))
            cursor = _add_months(signup, 1)
            while cursor <= DAY1:
                if rng.random() < 0.12:                       # 每月约一成账户调整席位
                    delta = int(rng.choice([-1, 1])) * int(rng.integers(1, 4))
                    if account.seats + delta > 0:
                        day = cursor + timedelta(days=int(rng.integers(0, 26)))
                        account.move(day, KIND_EXP if delta > 0 else KIND_CON, delta)
                if cursor > signup + timedelta(days=60) and rng.random() < churn_p:
                    account.churn(cursor + timedelta(days=int(rng.integers(0, 26))))
                    break
                cursor = _add_months(cursor, 1)
            accounts.append(account)
    return accounts


def _planted_accounts() -> list[Account]:
    """四组埋点账户 + 试用潮(s1 ~ s4;受影响账户集合两两不交,各自独占月份)。"""
    e001 = _new_account("ACC_E001", "企业版", DAY0, 24)     # s4:2 月流失(2 月零新签见 BACKGROUND)
    p001 = _new_account("ACC_P001", "专业版", DAY0, 22)     # s2:3 月一次性扩容,4 月无后续
    b001 = _new_account("ACC_B001", "基础版", DAY0, 500)    # s3:5 月降配 -450 席(末日口径唯一主因)
    f001 = _new_account("ACC_F001", "旗舰版", DAY0, 37)     # s1:6 月流失
    accounts = [e001, p001, b001, f001]
    e001.churn(date(2026, 2, 20))
    for day, delta in ((date(2025, 10, 12), 20), (date(2025, 12, 8), 15),
                       (date(2026, 1, 20), 10), (date(2026, 3, 8), 118)):
        p001.move(day, KIND_EXP, delta)
    b001.move(date(2026, 5, 28), KIND_CON, -450)
    f001.churn(date(2026, 6, 15))
    # s3 的干扰摆锤:试用潮(36 户 × 1 席;4 月初签约,5 月中旬到期流失)
    for i in range(BURST_COUNT):
        account = _new_account(f"ACC_T{901 + i:03d}", "试用版",
                               BURST_START + timedelta(days=i % 5), BURST_SEATS)
        account.churn(BURST_END + timedelta(days=i % 7))
        accounts.append(account)
    return accounts


def _write_tables(accounts: list[Account]) -> None:
    """由事件流导出四张表:变动明细 + 期快照(逐日累计)+ 两个维度表。"""
    subscription_rows, movement_rows, dim_rows = [], [], []
    dim_rows = [(a.account_id, a.account_name, a.plan, a.signup.isoformat(), a.seats)
                for a in accounts]
    for account in accounts:
        events = sorted(account.events)
        for day, kind, amount in events:
            movement_rows.append((day.isoformat(), account.account_id, kind, amount))
        index, level, day = 0, 0, account.signup
        while day <= DAY1:
            while index < len(events) and events[index][0] <= day:
                level += events[index][2]
                index += 1
            if level > 0:      # 水平为 0(流失后)不出行:没有行 = 没有订阅,不是缺失
                subscription_rows.append((day.isoformat(), account.account_id, level))
            day += timedelta(days=1)
    days = [DAY0 + timedelta(days=i) for i in range((DAY1 - DAY0).days + 1)]
    dates = [(d.isoformat(), d.isoformat(), d.year, d.month, d.day) for d in days]

    pd.DataFrame(subscription_rows, columns=["date_id", "account_id", "mrr_amount"]) \
        .to_parquet(DATA_DIR / "subscriptions.parquet", index=False)
    pd.DataFrame(movement_rows, columns=["date_id", "account_id", "movement_type", "amount"]) \
        .to_parquet(DATA_DIR / "mrr_movements.parquet", index=False)
    pd.DataFrame(dim_rows, columns=["account_id", "account_name", "plan",
                                    "signup_date", "seats"]) \
        .to_parquet(DATA_DIR / "dim_account.parquet", index=False)
    pd.DataFrame(dates, columns=["date_id", "date", "year", "month", "day"]) \
        .to_parquet(DATA_DIR / "dim_date.parquet", index=False)
    print(f"生成完毕: 账户 {len(accounts)} 户 / 快照 {len(subscription_rows)} 行 / "
          f"变动 {len(movement_rows)} 行 / 日期 {len(dates)} 天")
    print(f"Parquet 已写入 {DATA_DIR}")


def main() -> None:
    _write_tables(_background_accounts() + _planted_accounts())


if __name__ == "__main__":
    main()

    import duckdb  # noqa: E402  末尾自检:只依赖 duckdb,直接读刚写出的 parquet

    def parquet(table: str) -> str:
        return (DATA_DIR / f"{table}.parquet").as_posix()

    con = duckdb.connect()
    con.execute(f"CREATE VIEW sub AS SELECT * FROM read_parquet('{parquet('subscriptions')}')")
    con.execute(f"CREATE VIEW mov AS SELECT * FROM read_parquet('{parquet('mrr_movements')}')")
    con.execute(f"CREATE VIEW acc AS SELECT * FROM read_parquet('{parquet('dim_account')}')")
    con.execute(f"CREATE VIEW ddi AS SELECT * FROM read_parquet('{parquet('dim_date')}')")
    sub = "sub JOIN acc ON sub.account_id = acc.account_id"   # 快照 × 账户维度

    def one(sql: str) -> int:
        return con.execute(sql).fetchone()[0] or 0

    def rows(sql: str) -> list:
        return con.execute(sql).fetchall()

    def plan_level(plan: str, day: str) -> int:
        return one(f"SELECT SUM(sub.mrr_amount) FROM {sub}"
                   f" WHERE acc.plan = '{plan}' AND sub.date_id = '{day}'")

    print("\n==================== 内置验证 ====================")
    counts = ("SELECT (SELECT COUNT(*) FROM sub), (SELECT COUNT(*) FROM mov),"
              " (SELECT COUNT(*) FROM acc), (SELECT COUNT(*) FROM ddi)")
    print("表行数(快照/变动/账户/日期):", rows(counts)[0])

    # ① 铁律一:任意一天的「快照水平和」== 「该日及以前的全部变动」逐笔相加(精确对账)
    print("\n[① 变动 ↔ 快照对账](差必须恒为 0)")
    for day in ("2025-07-31", "2026-02-28", "2026-04-30", "2026-05-31", "2026-06-30"):
        snap = one(f"SELECT SUM(mrr_amount) FROM sub WHERE date_id = '{day}'")
        moves = one(f"SELECT SUM(amount) FROM mov WHERE date_id <= '{day}'")
        print(f"   {day}: 快照 {snap:,} 分 / 累计变动 {moves:,} 分 / 差 {snap - moves}")

    # ② 全量与各套餐的月末水平(末日口径)—— cases 里的水平数值都从这里来
    print("\n[② 月末水平(末日口径, 元)]")
    for day in ("2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30",
                "2026-05-31", "2026-06-30"):
        total = one(f"SELECT SUM(mrr_amount) FROM sub WHERE date_id = '{day}'")
        parts = rows(f"SELECT acc.plan, SUM(sub.mrr_amount) FROM {sub}"
                     f" WHERE sub.date_id = '{day}' GROUP BY acc.plan ORDER BY 2 DESC")
        print(f"   {day}: 全量 {total / 100:,.0f} | "
              + " ".join(f"{plan} {value / 100:,.0f}" for plan, value in parts))

    # ③ s3 的摆锤:试用潮(只统计摆锤账户 ACC_T9xx)
    #    末日水平口径:基期取 4 月末值、对比期取「到期前最后一天」值 → 每户变化 0(切片看不见它)
    #    逐日求和口径:4 月整月 vs 5 月半个月 → 求和后「掉」一大截(口径幻觉)
    print("\n[③ s3 摆锤:试用潮 ACC_T9xx(4 月 vs 5 月)]")
    cohort = ("SELECT sub.account_id AS a, sub.date_id AS d, SUM(sub.mrr_amount) AS v FROM "
              + sub + " WHERE acc.account_id LIKE 'ACC_T9%' GROUP BY a, d")
    # 切片口径 = 每账户在各窗口内取「最后有数据日」的值(与 slice_rows 一致)
    def slice_last(start: str, end: str) -> int:
        return one("SELECT SUM(v) FROM (SELECT a, v,"
                   " ROW_NUMBER() OVER (PARTITION BY a ORDER BY d DESC) AS rn"
                   f" FROM ({cohort}) WHERE d BETWEEN '{start}' AND '{end}') WHERE rn = 1")
    level = [slice_last("2026-04-01", "2026-04-30"), slice_last("2026-05-01", "2026-05-31")]
    summed = dict(rows(f"SELECT substr(d, 1, 7) AS m, SUM(v) FROM ({cohort})"
                       " GROUP BY m ORDER BY m"))
    churn = one("SELECT SUM(mov.amount) FROM mov JOIN acc ON mov.account_id = acc.account_id"
                " WHERE mov.movement_type = 'churn' AND acc.account_id LIKE 'ACC_T9%'")
    print(f"   切片末日值(每户取窗口内最后有数据日): 4 月 {level[0] / 100:,.0f} 元"
          f" / 5 月 {level[1] / 100:,.0f} 元"
          f" → 环比变化 {(level[1] - level[0]) / 100:+,.0f} 元(每户都是 299 元,进不了排名)")
    print(f"   逐日求和: 4 月 = {summed['2026-04'] / 100:,.0f} 元"
          f" / 5 月 = {summed['2026-05'] / 100:,.0f} 元"
          f" → 变化 {(summed['2026-05'] - summed['2026-04']) / 100:+,.0f} 元(口径幻觉)")
    print(f"   这 36 户 5 月到期流失,带走真实水平 {churn / 100:+,.0f} 元(存在,但不体现在任何切片的变化里)")

    # ④ 四组埋点的水平变化(cases 的数值锚点)
    print("\n[④ 埋点水平变化(末日口径)]")
    for label, plan, base, cmp in (("s4 企业版 2 月流失", "企业版", "2026-01-31", "2026-02-28"),
                                   ("s3 基础版 5 月降配", "基础版", "2026-04-30", "2026-05-31"),
                                   ("s1 旗舰版 6 月流失", "旗舰版", "2026-05-31", "2026-06-30")):
        first, last = (plan_level(plan, day) for day in (base, cmp))
        print(f"   {label}: {plan} {first / 100:,.0f} → {last / 100:,.0f} 元"
              f" ({last / 100 - first / 100:+,.0f} 元, {(last / first - 1) * 100:+.1f}%)")

    # ⑤ s2 的扩张塌陷:专业版的扩张流入按月拆(3 月有一次性扩容,4 月归零)
    print("\n[⑤ s2 扩张流入按月(专业版, 元)]")
    for month, value in rows(
            f"SELECT substr(mov.date_id, 1, 7) AS m, SUM(mov.amount) FROM mov"
            f" JOIN acc ON mov.account_id = acc.account_id"
            f" WHERE acc.plan = '专业版' AND mov.movement_type = 'expansion'"
            f" AND mov.date_id BETWEEN '2026-01-01' AND '2026-05-31' GROUP BY m ORDER BY m"):
        print(f"   {month}: {value / 100:+,.0f}")
    con.close()
    print("\n==================== 内置验证结束 ====================")
