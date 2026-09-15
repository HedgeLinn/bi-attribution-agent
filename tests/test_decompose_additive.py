"""加法分解(LMDI 的加性形态)的手算用例(REUSE_DESIGN §4.2)。

零残差的属性测试见 test_decompose_property.py;边界情形见 test_decompose_edges.py。
"""

import pytest

from attribution.decompose import decompose_additive
from tests.decompose_fixtures import _assert_zero_residual


def test_additive_hand_computed() -> None:
    """手算:V = 新增 + 流失;基期 60 + 40 = 100,对比期 70 + 50 = 120,ΔV = 20。

    加法分解不需要对数平均——效应就是分项自己的变化量:
        新增 70 − 60 = +10、流失 50 − 40 = +10 → Σ = 20 = ΔV ✓
    占比 = 10/20 = 0.5(两条各半);变化率 = 10/60 ≈ 1/6、10/40 = 0.25。
    """
    effects = decompose_additive(
        100.0, 120.0, {"new": 60.0, "churn": 40.0}, {"new": 70.0, "churn": 50.0}
    )
    assert [effect.effect for effect in effects] == [pytest.approx(10.0), pytest.approx(10.0)]
    assert all(effect.contribution == pytest.approx(0.5) for effect in effects)
    assert [effect.change_rate for effect in effects] == [pytest.approx(1 / 6), pytest.approx(0.25)]
    _assert_zero_residual(effects, 100.0, 120.0)

    # 允许负值(收缩、流失记负):60 − 10 − 40 = 10 → 70 − 10 − 50 = 10,ΔV = 0
    signed = decompose_additive(
        10.0, 10.0,
        {"add": 60.0, "shrink": -10.0, "churn": -40.0},
        {"add": 70.0, "shrink": -10.0, "churn": -50.0},
    )
    assert [effect.effect for effect in signed] == [
        pytest.approx(10.0), pytest.approx(0.0), pytest.approx(-10.0)
    ]
    assert all(effect.contribution is None for effect in signed)   # ΔV == 0 → 不除零
    _assert_zero_residual(signed, 10.0, 10.0)
