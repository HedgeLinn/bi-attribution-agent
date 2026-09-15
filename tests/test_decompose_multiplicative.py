"""乘法分解(LMDI)的手算用例、反例对照与顺序无关性(REUSE_DESIGN §4.2)。

零残差的属性测试见 test_decompose_property.py;边界情形见 test_decompose_edges.py。
"""

from math import prod

import pytest

from attribution.decompose import decompose_multiplicative
from tests.decompose_fixtures import (
    _TOLERANCE,
    _assert_zero_residual,
    _index,
    _sum_effects,
)


def test_multiplicative_hand_computed() -> None:
    """手算:V = A × B;基期 4×5 = 20,对比期 6×5 = 30,ΔV = 10。

    L(30, 20) = (30−20) / (ln30 − ln20) = 10 / ln1.5 = 10 / 0.4054651 = 24.663027
    效应_A = 24.663027 × ln(6/4) = 24.663027 × 0.4054651 = 10.000000   ← 恰好等于 ΔV
    效应_B = 24.663027 × ln(5/5) = 0                                   ← 未变化的因子天然为 0
    Σ = 10.000000 = ΔV ✓(B 没变,故全部变化归 A,交互项无从产生)
    """
    effects = decompose_multiplicative(20.0, 30.0, {"a": 4.0, "b": 5.0}, {"a": 6.0, "b": 5.0})
    by_factor = _index(effects, "factor")

    assert by_factor["a"].effect == pytest.approx(10.0, rel=_TOLERANCE)
    assert by_factor["b"].effect == pytest.approx(0.0, abs=_TOLERANCE)
    assert by_factor["a"].contribution == pytest.approx(1.0)      # 10 / 10
    assert by_factor["a"].change_rate == pytest.approx(0.5)       # (6−4)/4
    assert (by_factor["a"].base, by_factor["a"].cmp) == (4.0, 6.0)
    assert by_factor["a"].label == "a"                            # 未给 labels → 回落为因子名
    _assert_zero_residual(effects, 20.0, 30.0)


# ------------ 反例对照与顺序无关:为什么不用链式替代法 ------------
_DEMO_BASE = {"a": 2.0, "b": 3.0, "c": 5.0}      # V_0 = 30
_DEMO_CMP = {"a": 3.0, "b": 4.0, "c": 5.0}       # V_t = 60,ΔV = 30


def _chain_at_base(factors_base: dict, factors_cmp: dict) -> dict[str, float]:
    """链式替代法之一:每个因子单独变动、其余保持基期 → 交互项无处安放,留下残差。"""
    return {
        key: (factors_cmp[key] - factors_base[key])
        * prod(value for other, value in factors_base.items() if other != key)
        for key in factors_base
    }


def _chain_sequential(factors_base: dict, factors_cmp: dict, order: list) -> dict[str, float]:
    """链式替代法之二:按 order 逐个替换 → Σ 恒等于 ΔV,但每条效应取决于替换顺序。"""
    running = dict(factors_base)
    effects: dict[str, float] = {}
    previous = prod(running.values())
    for key in order:
        running[key] = factors_cmp[key]
        current = prod(running.values())
        effects[key] = current - previous
        previous = current
    return effects


def test_chain_substitution_has_residual_but_lmdi_has_none() -> None:
    """反例对照:同一组输入上链式替代法残差 ≠ 0,而 LMDI 残差 = 0。

    手算(A=2,B=3,C=5 → V_0 = 30;A=3,B=4,C=5 → V_t = 60;ΔV = 30):
        链式替代法(各自单独变动、其余保持基期):
            A: (3−2)×3×5 = 15   B: 2×(4−3)×5 = 10   C: 2×3×(5−5) = 0
            Σ = 25 → 残差 = 30 − 25 = 5,正是交互项 ΔA·ΔB·C_0 = 1×1×5
        LMDI:L(60,30) = 30/ln2 = 43.280854
            A: 43.280854×ln(3/2) = 17.548875   B: 43.280854×ln(4/3) = 12.451125
            C: 43.280854×ln(5/5) = 0
            Σ = 30.000000 → 残差 = 0(交互项被自动吸收,无需人为归属)
    """
    total_base, total_cmp = prod(_DEMO_BASE.values()), prod(_DEMO_CMP.values())
    assert (total_base, total_cmp) == (30.0, 60.0)

    chain_residual = (total_cmp - total_base) - sum(_chain_at_base(_DEMO_BASE, _DEMO_CMP).values())
    assert chain_residual == pytest.approx(5.0, rel=_TOLERANCE)     # 手算的交互项
    assert abs(chain_residual) > 1e-6                              # 真实残差,不是浮点噪声

    effects = decompose_multiplicative(total_base, total_cmp, _DEMO_BASE, _DEMO_CMP)
    by_factor = _index(effects, "factor")
    assert by_factor["a"].effect == pytest.approx(17.548875, rel=1e-6)
    assert by_factor["b"].effect == pytest.approx(12.451125, rel=1e-6)
    assert by_factor["c"].effect == pytest.approx(0.0, abs=_TOLERANCE)
    assert _sum_effects(effects) == pytest.approx(30.0, rel=_TOLERANCE)   # LMDI 残差 = 0


def test_factor_order_does_not_matter() -> None:
    """顺序无关:打乱因子顺序,每条效应不变(逐个替换的链式替代法恰好相反)。"""
    total_base, total_cmp = prod(_DEMO_BASE.values()), prod(_DEMO_CMP.values())
    reversed_keys = list(_DEMO_BASE)[::-1]
    forward = {e.factor: e.effect for e in decompose_multiplicative(total_base, total_cmp, _DEMO_BASE, _DEMO_CMP)}
    backward = {e.factor: e.effect for e in decompose_multiplicative(
        total_base, total_cmp,
        {key: _DEMO_BASE[key] for key in reversed_keys},
        {key: _DEMO_CMP[key] for key in reversed_keys},
    )}

    assert set(forward) == set(backward)                       # 结果集合相同
    for key, effect in forward.items():
        assert backward[key] == pytest.approx(effect, rel=_TOLERANCE, abs=_TOLERANCE)

    # 对照:链式替代法换顺序后每条效应都变(Σ 仍是 ΔV,但归属被人为改变)
    order_forward = _chain_sequential(_DEMO_BASE, _DEMO_CMP, ["a", "b", "c"])
    order_backward = _chain_sequential(_DEMO_BASE, _DEMO_CMP, ["c", "b", "a"])
    assert sum(order_forward.values()) == pytest.approx(30.0, rel=_TOLERANCE)
    assert order_forward["a"] != pytest.approx(order_backward["a"])
    assert order_forward["b"] != pytest.approx(order_backward["b"])
