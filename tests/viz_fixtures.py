# -*- coding: utf-8 -*-
"""可视化测试的共享夹具:容器替身、事件构造器、真实引擎结果。

从 `tests/test_attribution_viz.py` 迁出,供拆分后的多个测试文件复用 —— 否则每个
文件都要复制一份 Stub 与真实结果(引擎 fixture 是 module scope,只跑一遍)。
"""

from __future__ import annotations

import pytest

from tests.engine_fixtures import BASE_WINDOW, CMP_WINDOW, real_engine


class Stub:
    """st 容器替身:渲染调用记成 (方法名, *args, **kwargs);调用清单本身即断言对象。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name: str):
        return lambda *args, **kwargs: self.calls.append((name, *args, kwargs))


def call(step, name, args, result) -> list[dict]:
    """一步工具调用 -> 两条事件(tool_call + tool_result),与 harness/loop.py 同构。"""
    return [{"type": "tool_call", "step": step, "name": name, "args": args},
            {"type": "tool_result", "step": step, "name": name, "result": result}]


def cont(step, metric, level, result, **extra) -> list[dict]:
    """一步 contribute 事件(dimension 固定 store;extra 可带 filters)。"""
    return call(step, "contribute",
                {"metric": metric, "dimension": "store", "level": level, **extra}, result)


@pytest.fixture(scope="module")
def real() -> dict:
    """真实结果(引擎只跑一遍);error 由引擎**真抛**的异常取来 —— 失败的真实形态。"""
    engine = real_engine()
    window = (*BASE_WINDOW, *CMP_WINDOW)           # contribute 的点位参数顺序:基期、对比期
    out = {"anomaly": engine.detect_anomaly("gmv", *CMP_WINDOW, *BASE_WINDOW),
           "region": engine.contribute("gmv", "store", "region", *window),
           "city": engine.contribute("gmv", "store", "city", *window),
           "store": engine.contribute("gmv", "store", "store_id", *window),
           "decompose": engine.decompose("gmv", ["aov", "orders_count"], *window)}
    with pytest.raises(ValueError) as raised:
        engine.contribute("gmv", "store", "store_name", *window)
    out["error"] = {"error": str(raised.value)}
    return out


@pytest.fixture(scope="module")
def events(real) -> list[dict]:
    """真实归因事件流:检测 -> 区域贡献 -> 下钻到城市(filters 命中切片)-> 因子分解。"""
    return [*call(0, "detect_anomaly", {"metric": "gmv"}, real["anomaly"]),
            *cont(1, "gmv", "region", real["region"]),
            *cont(2, "gmv", "city", real["city"],
                  filters={"region": real["region"]["top"][0]["key"]}),
            *call(3, "decompose", {"target": "gmv"}, real["decompose"])]