"""结构分解(权重 × 强度)的手算用例(REUSE_DESIGN §4.3)。

「结构效应 vs 自身效应」是这套算法最锋利的一刀:同样是总量下降,结构效应为主意味着
「高价商品没了」,自身效应为主才意味着「降价了」。零残差的属性测试见
test_decompose_property.py;边界情形见 test_decompose_edges.py。
"""

import pytest

from attribution.decompose import decompose_structural
from tests.decompose_fixtures import _TOLERANCE, _assert_zero_residual, _index


def test_structural_only_weights_changed() -> None:
    """§4.3 的核心问题:各切片自身强度都没变、只有权重变了 → **结构效应吃掉全部变化**。

    手算(权重 0.5/0.5 → 0.8/0.2,强度恒为 a=10、b=20):
        V_0 = 0.5×10 + 0.5×20 = 15;V_t = 0.8×10 + 0.2×20 = 12;ΔV = −3
        切片 a:V_0 = 5,V_t = 8 → L(8,5) = (8−5)/ln(8/5) = 3/0.4700036 = 6.3829787
                结构项 = 6.3829787 × ln(0.8/0.5) = 6.3829787 × 0.4700036 = +3.0000000
                自身项 = 6.3829787 × ln(10/10) = 0
        切片 b:V_0 = 10,V_t = 4 → L(4,10) = (4−10)/ln(4/10) = −6/(−0.9162907) = 6.5481400
                结构项 = 6.5481400 × ln(0.2/0.5) = −6.0000000;自身项 = 0
        结构效应 = 3 − 6 = −3(占比 100%),自身效应 = 0 → **不是降价,是结构变了**
    """
    effects = decompose_structural(
        15.0, 12.0,
        {"a": 0.5, "b": 0.5}, {"a": 0.8, "b": 0.2},
        {"a": 10.0, "b": 20.0}, {"a": 10.0, "b": 20.0},
    )
    by_label = _index(effects, "label")

    assert by_label["结构效应"].effect == pytest.approx(-3.0, rel=_TOLERANCE)
    assert by_label["自身效应"].effect == pytest.approx(0.0, abs=_TOLERANCE)
    assert by_label["结构效应"].contribution == pytest.approx(1.0)    # 吃掉了全部变化
    assert by_label["自身效应"].contribution == pytest.approx(0.0)
    _assert_zero_residual(effects, 15.0, 12.0)

    # 镜像情形:权重不变、强度全变 → 全部变化来自自身效应,结构效应为 0
    own = decompose_structural(
        15.0, 16.5,
        {"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5},
        {"a": 10.0, "b": 20.0}, {"a": 11.0, "b": 22.0},
    )
    by_label = _index(own, "label")
    assert by_label["结构效应"].effect == pytest.approx(0.0, abs=_TOLERANCE)
    assert by_label["自身效应"].effect == pytest.approx(1.5, rel=_TOLERANCE)
    _assert_zero_residual(own, 15.0, 16.5)

    # labels 覆盖缺省展示名(结构分解两条汇总项的键名由 docstring 冻结)
    mix, rate = decompose_structural(
        15.0, 12.0,
        {"a": 0.5, "b": 0.5}, {"a": 0.8, "b": 0.2},
        {"a": 10.0, "b": 20.0}, {"a": 10.0, "b": 20.0},
        labels={"__mix__": "结构", "__rate__": "自身"},
    )
    assert (mix.factor, mix.label) == ("__mix__", "结构")
    assert (rate.factor, rate.label) == ("__rate__", "自身")


def test_log_mean_limit_for_unchanged_slice() -> None:
    """L(a, a) 的极限:切片自身 V_i 未变时取 L = V_i,而不是 0/0。

    手算:切片 a 权重 0.5→0.25、强度 10→20,则 V_a 前后都是 5(走 a == b 分支);
          切片 b 权重 0.5→0.75、强度 20→20,则 V_b 由 10 变 15。
        V_0 = 15,V_t = 20,ΔV = 5
        切片 a:L(5,5) = 5 → 结构项 = 5×ln(0.25/0.5) = −3.465736,自身项 = 5×ln(20/10) = +3.465736
        切片 b:L(15,10) = 5/ln1.5 = 12.330777 → 结构项 = 12.330777×ln(0.75/0.5) = +5.000000,自身项 = 0
        结构效应 = −3.465736 + 5.000000 = 1.534264;自身效应 = 3.465736;Σ = 5.000000 = ΔV ✓
    """
    effects = decompose_structural(
        15.0, 20.0,
        {"a": 0.5, "b": 0.5}, {"a": 0.25, "b": 0.75},
        {"a": 10.0, "b": 20.0}, {"a": 20.0, "b": 20.0},
    )
    by_label = _index(effects, "label")

    assert by_label["结构效应"].effect == pytest.approx(1.534264, rel=1e-6)
    assert by_label["自身效应"].effect == pytest.approx(3.465736, rel=1e-6)
    _assert_zero_residual(effects, 15.0, 20.0)


def test_weight_sum_within_tolerance_is_allowed() -> None:
    """权重和落在容差内则放行(浮点归一化后的 1/3 × 3 就是这种情况)。"""
    third = 1 / 3
    effects = decompose_structural(
        10.0, 11.0,
        {"a": third, "b": third, "c": third}, {"a": third, "b": third, "c": third},
        {"a": 10.0, "b": 10.0, "c": 10.0}, {"a": 11.0, "b": 11.0, "c": 11.0},
    )
    by_label = _index(effects, "label")
    assert by_label["结构效应"].effect == pytest.approx(0.0, abs=_TOLERANCE)
    assert by_label["自身效应"].effect == pytest.approx(1.0, rel=1e-6)
    _assert_zero_residual(effects, 10.0, 11.0)
