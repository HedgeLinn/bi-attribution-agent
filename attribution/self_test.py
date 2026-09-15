"""归因引擎自测脚本:用真实 Parquet 数据验证契约三个自测点。

运行:  python attribution/self_test.py
返回码: 0 = 全部通过;1 = 有失败
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from attribution.engine import AttributionEngine
from harness.datasets import resolve_dataset

# 数据与语义层统一由数据集包解析(M2,§3.3):不再写死 data/ 与 semantic/semantic.yaml。
# datasets_dir 用绝对路径,保证从任何目录运行都能找到数据集。
# 本脚本是 ecommerce-demo 的专属验收:数据集 id 显式钉死(仓库里有多个数据集后,
# 「唯一数据集自动选中」不再适用,必须显式指定)
DATASET = resolve_dataset("ecommerce-demo", datasets_dir=os.path.join(ROOT, "datasets"))
DATA_DIR = str(DATASET.data_dir)
SEMANTIC = str(DATASET.semantic_path)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"[PASS] {name} {detail}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


def main():
    eng = AttributionEngine(DATA_DIR, SEMANTIC)

    print("=" * 70)
    print("自测点 1:query_metric('gmv', ['region'], {}, '2026-06-01','2026-06-30')")
    print("=" * 70)
    r1 = eng.query_metric("gmv", ["region"], {}, "2026-06-01", "2026-06-30")
    print(json.dumps(r1, ensure_ascii=False, indent=2))
    check("query_metric 行数>0", len(r1["rows"]) > 0)
    check("query_metric 各 region 有值",
          all(isinstance(x["value"], (int, float)) and x["value"] > 0 for x in r1["rows"]))

    print()
    print("=" * 70)
    print("自测点 2:contribute('gmv','store','store_id',"
          "'2026-05-01','2026-05-31','2026-06-01','2026-06-30',top_k=5)")
    print("=" * 70)
    r2 = eng.contribute("gmv", "store", "store_id",
                        "2026-05-01", "2026-05-31",
                        "2026-06-01", "2026-06-30", top_k=5)
    print(json.dumps(r2, ensure_ascii=False, indent=2))
    top0 = r2["top"][0]
    check("contribute top[0].label 为「上海徐家汇旗舰店」", top0["label"] == "上海徐家汇旗舰店",
          f"实际 label={top0['label']}")
    check("contribute top[0] change 显著为负", top0["change"] < -500000,
          f"实际 change={top0['change']}")
    check("contribute 含 contribution", "contribution" in top0 and top0["contribution"] is not None)

    print()
    print("=" * 70)
    print("自测点 3:detect_anomaly('gmv','2026-06-01','2026-06-30','2026-05-01','2026-05-31')")
    print("=" * 70)
    r3 = eng.detect_anomaly("gmv", "2026-06-01", "2026-06-30", "2026-05-01", "2026-05-31")
    print(json.dumps(r3, ensure_ascii=False, indent=2))
    check("detect_anomaly is_anomaly=True", r3["is_anomaly"] is True,
          f"change_rate={r3['change_rate']}")

    print()
    print("=" * 70)
    print("附加抽查:derived 指标 aov/refund_rate/discount_rate + 其它维度下钻")
    print("=" * 70)
    r4 = eng.contribute("aov", "store", "region",
                        "2026-05-01", "2026-05-31",
                        "2026-06-01", "2026-06-30", top_k=3)
    print("aov by region contribute:", json.dumps(r4, ensure_ascii=False))
    check("derived contribution 一律 None",
          all(x.get("contribution") is None for x in r4["top"]))
    check("derived 含 change_rate",
          all("change_rate" in x for x in r4["top"]))

    r5 = eng.contribute("refund_rate", "product", "category",
                        "2026-05-01", "2026-05-31",
                        "2026-06-01", "2026-06-30", top_k=3)
    print("refund_rate by category contribute:", json.dumps(r5, ensure_ascii=False))

    r6 = eng.contribute("sales_qty", "channel", "channel_id",
                        "2026-05-01", "2026-05-31",
                        "2026-06-01", "2026-06-30", top_k=3)
    print("sales_qty by channel_id contribute:", json.dumps(r6, ensure_ascii=False))
    check("channel 维度 key 层 label 用 channel_name",
          all(x["label"] not in (None, "") for x in r6["top"]))

    r7 = eng.query_metric("orders_count", ["year"], {}, "2026-06-01", "2026-06-30")
    print("orders_count by year:", json.dumps(r7, ensure_ascii=False))

    r8 = eng.query_metric("aov", ["region"], {}, "2026-06-01", "2026-06-30")
    print("aov by region:", json.dumps(r8, ensure_ascii=False))

    r9 = eng.query_metric("gmv", [], {"store_id": "STORE_S0001"}, "2026-06-01", "2026-06-30")
    print("gmv filtered store:", json.dumps(r9, ensure_ascii=False))
    check("带过滤查询可用", r9["total"] is not None and r9["total"] > 0)

    print()
    print("=" * 70)
    print(f"结果:通过 {len(PASS)} 项,失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("自测全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
