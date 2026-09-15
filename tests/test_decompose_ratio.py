"""比率分解(分子 / 分母)的手算用例(REUSE_DESIGN §4.2)。

零残差的属性测试见 test_decompose_property.py;边界情形见 test_decompose_edges.py。
"""

import pytest

from attribution.decompose import decompose_ratio
from tests.decompose_fixtures import _TOLERANCE, _assert_zero_residual, _index


def test_ratio_hand_computed() -> None:
    """手算:转化率 = 分子 / 分母;基期 1000/100 = 10,对比期 900/100 = 9,ΔV = −1。

    L(9, 10) = (9−10) / (ln9 − ln10) = −1 / (−0.1053605) = 9.4912216
    分子效应 = 9.4912216 × ln(900/1000) = 9.4912216 × (−0.1053605) = −1.000000  ← 全部来自分子
    分母效应 = 9.4912216 × ln(100/100) = 0                                       ← 分母没变
    Σ = −1.000000 = ΔV ✓(分母因子取倒数,故其对数比写作 ln(D_0/D_t))
    """
    effects = decompose_ratio(1000.0, 900.0, 100.0, 100.0)
    by_factor = _index(effects, "factor")

    assert by_factor["numerator"].effect == pytest.approx(-1.0, rel=_TOLERANCE)
    assert by_factor["denominator"].effect == pytest.approx(0.0, abs=_TOLERANCE)
    assert by_factor["numerator"].contribution == pytest.approx(1.0)
    # base / cmp 报原始观测值(分子 1000→900、分母 100→100),而不是倒数
    assert (by_factor["denominator"].base, by_factor["denominator"].cmp) == (100.0, 100.0)
    assert by_factor["denominator"].label == "分母效应"          # 缺省展示名
    _assert_zero_residual(effects, 10.0, 9.0)
