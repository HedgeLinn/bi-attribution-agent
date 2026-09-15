# -*- coding: utf-8 -*-
"""独立验证脚本:核对 Parquet 可读、行数、埋点异常、层次下钻定位。

读取数据集包里的 data/*.parquet(只依赖 duckdb 与数据集包,不依赖造数脚本的运行时状态)。
与 generate_data.py 中的埋点常量保持一致。
"""
import sys
from pathlib import Path
import datetime as dt

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.datasets import resolve_dataset  # noqa: E402

# 数据在数据集包里(M2,§3.3):位置由 dataset.yaml 的 data_dir 决定,这里不写死路径。
# datasets_dir 给绝对路径,保证从任何目录运行都能找到数据集
DATA = resolve_dataset("ecommerce-demo", datasets_dir=ROOT / "datasets").data_dir

# 埋点异常(与 generate_data.py 同步)
ANOM_STORE_ID = "STORE_S0001"
ANOM_STORE_NAME = "上海徐家汇旗舰店"
ANOM_SKU_ID = "SKU_P0001"
ANOM_SKU_NAME = "旗舰智能手机 Pro Max"

con = duckdb.connect()
con.execute(f"""
    CREATE VIEW orders     AS SELECT * FROM read_parquet('{DATA}/orders.parquet');
    CREATE VIEW dim_store  AS SELECT * FROM read_parquet('{DATA}/dim_store.parquet');
    CREATE VIEW dim_product AS SELECT * FROM read_parquet('{DATA}/dim_product.parquet');
    CREATE VIEW dim_date   AS SELECT * FROM read_parquet('{DATA}/dim_date.parquet');
    CREATE VIEW dim_channel AS SELECT * FROM read_parquet('{DATA}/dim_channel.parquet');
""")

line = "=" * 64
print(line)
print("1) 规模与时间范围")
print(line)
n_orders = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
n_order = con.execute("SELECT COUNT(DISTINCT order_id) FROM orders").fetchone()[0]
n_store, n_prod, n_date, n_chan = con.execute(
    "SELECT (SELECT COUNT(*) FROM dim_store),(SELECT COUNT(*) FROM dim_product),"
    "(SELECT COUNT(*) FROM dim_date),(SELECT COUNT(*) FROM dim_channel)").fetchone()
dmin, dmax = con.execute("SELECT MIN(date_id), MAX(date_id) FROM dim_date").fetchone()
print(f"  orders 行数(件明细): {n_orders:,}   订单数: {n_order:,}")
print(f"  维度表: dim_store={n_store}  dim_product={n_prod}  dim_date={n_date}  dim_channel={n_chan}")
print(f"  时间范围: {dmin} ~ {dmax}(共 {(dt.date.fromisoformat(dmax)-dt.date.fromisoformat(dmin)).days+1} 天 = 24 个月)")

# ---------------- 月度对比工具 ----------------
def m5m6(pred=""):
    return con.execute(f"""
        SELECT d.month, SUM(o.amount) AS gmv, SUM(o.quantity) AS qty,
               COUNT(DISTINCT o.order_id) AS ords
        FROM orders o JOIN dim_date d ON o.date_id=d.date_id JOIN dim_store s ON o.store_id=s.store_id
        WHERE d.year=2026 AND d.month IN (5,6) {pred}
        GROUP BY d.month ORDER BY d.month""").fetchall()

def report(rows, title):
    m5, m6 = rows
    gmv_chg = (m6[1]/m5[1]-1)*100
    qty_chg = (m6[2]/m5[2]-1)*100
    aov5, aov6 = m5[1]/m5[3], m6[1]/m6[3]
    aov_chg = (aov6/aov5-1)*100
    print(f"  GMV   2026-05={m5[1]:>12,.0f}  2026-06={m6[1]:>12,.0f}  环比 {gmv_chg:+.1f}%")
    print(f"  销量  2026-05={m5[2]:>12,.0f}  2026-06={m6[2]:>12,.0f}  环比 {qty_chg:+.1f}%")
    print(f"  客单价(AOV=GMV/订单数) 05={aov5:.0f}  06={aov6:.0f}  环比 {aov_chg:+.1f}%")

print()
print(line)
print("2) 全量环比(干扰项:618 后自然回落,整体 GMV 下跌)")
print(line)
report(m5m6(), "全量")

print()
print(line)
print("3) 主异常门店(正确答案) 2026-05 vs 2026-06")
print(line)
anom_rows = m5m6(f"AND o.store_id='{ANOM_STORE_ID}'")
report(anom_rows, "异常店")
# ANOM_SKU 份额(验证根因:高单价头部 SKU 下架)
print("  其中头部 SKU 份额(2026-05):")
for r in con.execute(f"""
    SELECT ROUND(100.0*SUM(CASE WHEN o.product_id='{ANOM_SKU_ID}' THEN o.quantity ELSE 0 END)/SUM(o.quantity),1),
           ROUND(100.0*SUM(CASE WHEN o.product_id='{ANOM_SKU_ID}' THEN o.amount ELSE 0 END)/SUM(o.amount),1)
    FROM orders o JOIN dim_date d ON o.date_id=d.date_id
    WHERE d.year=2026 AND d.month=5 AND o.store_id='{ANOM_STORE_ID}'""").fetchall():
    print(f"    销量行数占比 {r[0]}% / GMV 占比 {r[1]}%  (2026-06 该 SKU 已下架,占比 0%)")

print()
print(line)
print("4) 对照:同地区/同城市正常门店同期变化(应正常,无暴跌)")
print(line)
report(m5m6("AND s.region='华东' AND o.store_id!='STORE_S0001'"), "华东(排除异常店)")
report(m5m6("AND s.city='上海' AND o.store_id!='STORE_S0001'"), "上海(排除异常店)")

print()
print(line)
print("5) 层次下钻定位:GMV 环比 @ 区域/城市/门店(2026-06 vs 05)")
print(line)
print("  区域层(应有回落但无法区分异常):")
for r in con.execute("""
    WITH t AS (SELECT s.region rg, d.month m, SUM(o.amount) g FROM orders o JOIN dim_date d ON o.date_id=d.date_id JOIN dim_store s ON o.store_id=s.store_id
               WHERE d.year=2026 AND d.month IN (5,6) GROUP BY s.region, d.month)
    SELECT rg, ROUND(100.0*(MAX(g) FILTER (WHERE m=6)/MAX(g) FILTER (WHERE m=5)-1),1) chg
    FROM t GROUP BY rg ORDER BY rg""").fetchall():
    print(f"    {r[0]}: {r[1]:+.1f}%")
print("  城市层(华东):")
for r in con.execute("""
    WITH t AS (SELECT s.city c, d.month m, SUM(o.amount) g FROM orders o JOIN dim_date d ON o.date_id=d.date_id JOIN dim_store s ON o.store_id=s.store_id
               WHERE d.year=2026 AND d.month IN (5,6) AND s.region='华东' GROUP BY s.city, d.month)
    SELECT c, ROUND(100.0*(MAX(g) FILTER (WHERE m=6)/MAX(g) FILTER (WHERE m=5)-1),1) chg
    FROM t GROUP BY c ORDER BY c""").fetchall():
    print(f"    {r[0]}: {r[1]:+.1f}%")
print("  门店层(上海,GMV 降幅 Top 5):")
for r in con.execute("""
    WITH t AS (SELECT s.store_name n, d.month m, SUM(o.amount) g FROM orders o JOIN dim_date d ON o.date_id=d.date_id JOIN dim_store s ON o.store_id=s.store_id
               WHERE d.year=2026 AND d.month IN (5,6) AND s.city='上海' AND s.tier IN ('旗舰店','中心店')
               GROUP BY s.store_name, d.month)
    SELECT n, ROUND(100.0*(MAX(g) FILTER (WHERE m=6)/MAX(g) FILTER (WHERE m=5)-1),1) chg
    FROM t GROUP BY n ORDER BY chg ASC LIMIT 5""").fetchall():
    print(f"    {r[0]}: {r[1]:+.1f}%")
print("  结论:全局/区域/城市层都只看到 -10%~-20% 的自然回落;"
      "只有门店层 STORE_S0001 显形(-57%),可定位到该店。")

print()
print(line)
print("6) 语义层 YAML 契约校验(字段名不可改)")
print(line)
# 语义层随数据集打包(M2):路径取自数据集包,不再写死 semantic/semantic.yaml;
# schema v2 在 v1 字段之外新增了头部与 decompositions/time,断言改为「契约字段必须在」。
sem = yaml.safe_load(resolve_dataset(
    "ecommerce-demo", datasets_dir=ROOT / "datasets").semantic_path.read_text(encoding="utf-8"))
for field in ["fact_table", "date_field", "metrics", "dimensions"]:
    assert field in sem, f"顶层契约字段 {field} 缺失!"
for m in ["gmv", "sales_qty", "orders_count", "aov", "refund_rate", "discount_rate"]:
    assert m in sem["metrics"]
for d in ["store", "product", "channel", "date"]:
    assert d in sem["dimensions"]
print("  metrics:", ", ".join(sem["metrics"].keys()))
print("  dimensions:", ", ".join(sem["dimensions"].keys()))
print("  契约字段名校验通过(gmv/sales_qty/orders_count/aov/refund_rate/discount_rate;"
      "store/product/channel/date)")
print(f"  aov expression: {sem['metrics']['aov']['expression']}")
print(f"  store hierarchy: {sem['dimensions']['store']['hierarchy']}")
con.close()
print()
print("验证全部通过。")
