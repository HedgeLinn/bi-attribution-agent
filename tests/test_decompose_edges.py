"""边界情形与非法的输入(docstring 冻结的行为清单)。

正常的手算用例见 test_decompose_{multiplicative,additive,ratio,structural}.py。
"""

import pytest

from attribution.decompose import (
    _DELTA_SUBSTITUTE,          # δ 替代量:验证「替代后基准量」时必须用它复现
    DecomposeError,
    decompose_additive,
    decompose_multiplicative,
    decompose_ratio,
    decompose_structural,
)
from tests.decompose_fixtures import _TOLERANCE, _index, _sum_effects


def test_non_positive_factor_uses_delta_and_marks_label() -> None:
    """因子为 0 或负:δ 替代 + label 标注;基准量随之改为替代后的重建量(结果为近似)。

    手算:因子 a 由 0 → 2,因子 b 恒为 3。
        δ 替代后基期 a = δ,故 V_0' = 3δ ≈ 0、V_t' = 6;Σ 效应 ≡ V_t' − V_0' ≈ 6 − 3e-12,
        与真实 ΔV = 6 − 0 只差 δ 量级(替代是近似的,故 label 必须标注)。
    """
    effects = decompose_multiplicative(0.0, 6.0, {"a": 0.0, "b": 3.0}, {"a": 2.0, "b": 3.0})
    by_factor = _index(effects, "factor")
    expected = 2.0 * 3.0 - _DELTA_SUBSTITUTE * 3.0

    assert "近似" in by_factor["a"].label                  # 被替代的因子必须标注
    assert "近似" not in by_factor["b"].label              # 未替代的因子不标注
    assert (by_factor["a"].base, by_factor["a"].cmp) == (0.0, 2.0)   # 报原始观测值
    assert _sum_effects(effects) == pytest.approx(expected, rel=_TOLERANCE)

    # 负值同样走 δ 替代:结果对应替代后的重建量,而不是无定义的 ln(负数)
    negative = decompose_multiplicative(-6.0, 6.0, {"a": -2.0, "b": 3.0}, {"a": 2.0, "b": 3.0})
    assert "近似" in negative[0].label
    assert _sum_effects(negative) == pytest.approx(expected, rel=_TOLERANCE)


def test_zero_total_change_sets_contribution_none() -> None:
    """契约:total_change == 0 时 contribution 置 None(不做除零)。

    乘法:V_t == V_0(2×3 = 3×2 = 6)→ 对数平均分母为 0 → 全部 effect 置 0;
    比率:分子分母同比例变化(10 → 10)→ 两条效应置 0;
    结构:两期权重与强度全同 → 两条效应置 0。
    """
    multiplicative = decompose_multiplicative(6.0, 6.0, {"a": 2.0, "b": 3.0}, {"a": 3.0, "b": 2.0})
    assert [effect.effect for effect in multiplicative] == [0.0, 0.0]
    assert all(effect.contribution is None for effect in multiplicative)

    ratio = decompose_ratio(1000.0, 1100.0, 100.0, 110.0)
    assert [effect.effect for effect in ratio] == [0.0, 0.0]
    assert all(effect.contribution is None for effect in ratio)

    structural = decompose_structural(
        15.0, 15.0, {"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5},
        {"a": 10.0, "b": 20.0}, {"a": 10.0, "b": 20.0},
    )
    assert all(effect.effect == 0.0 and effect.contribution is None for effect in structural)


def test_invalid_inputs_raise() -> None:
    """非法输入一律抛 DecomposeError,而不是静默返回带残差 / 无意义的数。"""
    with pytest.raises(DecomposeError):        # 因子集合不一致:只在一侧的因子无从配对
        decompose_multiplicative(6.0, 12.0, {"a": 2.0, "b": 3.0}, {"a": 3.0, "c": 4.0})
    with pytest.raises(DecomposeError):
        decompose_additive(100.0, 100.0, {"x": 60.0, "y": 40.0}, {"x": 60.0})

    with pytest.raises(DecomposeError):        # 分母为 0:比率本身无定义
        decompose_ratio(100.0, 100.0, 0.0, 50.0)
    with pytest.raises(DecomposeError):
        decompose_ratio(100.0, 100.0, 50.0, 0.0)

    with pytest.raises(DecomposeError):        # 权重切片集合两期不一致
        decompose_structural(
            15.0, 15.0, {"a": 0.5, "b": 0.5}, {"a": 1.0}, {"a": 10.0, "b": 20.0}, {"a": 10.0}
        )
    with pytest.raises(DecomposeError):        # 权重与强度的切片集合不一致
        decompose_structural(
            15.0, 15.0, {"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}, {"a": 10.0}, {"a": 10.0}
        )
    with pytest.raises(DecomposeError):        # 权重是占比,和必须为 1(此处误按百分数传入)
        decompose_structural(
            100.0, 100.0, {"a": 50.0, "b": 50.0}, {"a": 50.0, "b": 50.0},
            {"a": 1.0, "b": 1.0}, {"a": 1.0, "b": 1.0},
        )

    with pytest.raises(DecomposeError):        # 恒等式不成立:Π f = 6,却声称总量 20 → 30
        decompose_multiplicative(20.0, 30.0, {"a": 2.0, "b": 3.0}, {"a": 2.0, "b": 5.0})
    with pytest.raises(DecomposeError):        # 加法:Σ p = 100,却声称总量 200
        decompose_additive(200.0, 200.0, {"x": 60.0, "y": 40.0}, {"x": 70.0, "y": 30.0})
    with pytest.raises(DecomposeError):        # 结构:Σ w·r = 15,却声称总量 100
        decompose_structural(
            100.0, 100.0, {"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5},
            {"a": 10.0, "b": 20.0}, {"a": 10.0, "b": 20.0},
        )
