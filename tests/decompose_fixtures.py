"""attribution.decompose 单测的共享夹具:断言与随机输入构造。

被 test_decompose_*.py 各文件共用,免得同一批辅助函数在几处各写一遍后互相漂移。
本模块不是测试文件(pytest 只收集 test_*.py),不会产生用例。

原本这些辅助函数与全部用例挤在 test_decompose.py(426 行,超出「单文件 ≤300 行」约定),
按分解种类拆分时把公共部分抽到这里;**用例一个都没少**,只是换了文件放。
"""

import random
from math import prod

import pytest

# Σ effect 与 ΔV 的差只允许来自双精度舍入(实现里没有任何四舍五入或事后修正)
_TOLERANCE = 1e-9
# 属性测试组数:种子固定 → 失败可复现
_PROPERTY_SEEDS = 100


def _sum_effects(effects: list) -> float:
    """Σ effect —— 零残差断言的对象。"""
    return sum(effect.effect for effect in effects)


def _assert_zero_residual(effects: list, total_base: float, total_cmp: float) -> None:
    """零残差:Σ effect ≡ total_cmp − total_base。"""
    assert _sum_effects(effects) == pytest.approx(
        total_cmp - total_base, rel=_TOLERANCE, abs=_TOLERANCE
    )


def _index(effects: list, field: str) -> dict:
    """按 factor 或 label 建索引,便于逐条断言。"""
    return {getattr(effect, field): effect for effect in effects}


def _unit_weights(rng: random.Random, count: int) -> dict[str, float]:
    """随机权重并归一化到和为 1(V = Σ w·r 的前提)。"""
    raw = [rng.uniform(0.1, 1.0) for _ in range(count)]
    total = sum(raw)
    return {f"slice_{index}": value / total for index, value in enumerate(raw)}


def _weighted_total(weights: dict, rates: dict) -> float:
    """Σ w·r —— 与模块内部同样的累加顺序(顺序一致,恒等式校验才能精确对上)。"""
    return sum(weights[key] * rates[key] for key in weights)


def _random_multiplicative(rng: random.Random) -> tuple:
    """随机乘法输入:总量由因子相乘得出,恒等式 V = Π f 天然成立。"""
    factors_base = {f"factor_{i}": rng.uniform(0.2, 50.0) for i in range(rng.randint(1, 5))}
    factors_cmp = {key: value * rng.uniform(0.2, 3.0) for key, value in factors_base.items()}
    return prod(factors_base.values()), prod(factors_cmp.values()), factors_base, factors_cmp


def _random_additive(rng: random.Random) -> tuple:
    """随机加法输入:分项允许负值,总量由分项求和得出。"""
    keys = [f"part_{i}" for i in range(rng.randint(1, 5))]
    parts_base = {key: rng.uniform(-100.0, 100.0) for key in keys}
    parts_cmp = {key: rng.uniform(-100.0, 100.0) for key in keys}
    return sum(parts_base.values()), sum(parts_cmp.values()), parts_base, parts_cmp


def _random_ratio(rng: random.Random) -> tuple[float, float, float, float]:
    """随机比率输入 (N_0, N_t, D_0, D_t):分母远离 0,V = N/D 由两者算出。"""
    return (
        rng.uniform(1.0, 1000.0), rng.uniform(1.0, 1000.0),
        rng.uniform(1.0, 1000.0), rng.uniform(1.0, 1000.0),
    )


def _random_structural(rng: random.Random) -> tuple:
    """随机结构输入:权重和为 1,总量 = Σ w·r;一半切片的自身强度保持不变。"""
    count = rng.randint(1, 4)
    weights_base, weights_cmp = _unit_weights(rng, count), _unit_weights(rng, count)
    rates_base = {key: rng.uniform(1.0, 100.0) for key in weights_base}
    rates_cmp = {
        key: value if rng.random() < 0.5 else value * rng.uniform(0.5, 2.0)
        for key, value in rates_base.items()
    }
    base = _weighted_total(weights_base, rates_base)
    cmp_ = _weighted_total(weights_cmp, rates_cmp)
    return base, cmp_, weights_base, weights_cmp, rates_base, rates_cmp
