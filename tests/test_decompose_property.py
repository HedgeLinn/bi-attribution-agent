"""LMDI 四种分解的**属性测试**:随机输入上的零残差(REUSE_DESIGN §4.2 / §7①)。

**核心断言是零残差**:`Σ effect ≡ total_cmp − total_base`——这是模块存在的理由,
链式替代法做不到(反例对照见 test_decompose_multiplicative.py)。
这是「零残差不是碰巧」的主证据:100 组固定随机种子,四种分解各跑一遍。
手算用例与边界清单见 test_decompose_{multiplicative,additive,ratio,structural,edges}.py。
"""

import random

import pytest

from attribution.decompose import (
    decompose_additive,
    decompose_multiplicative,
    decompose_ratio,
    decompose_structural,
)
from tests.decompose_fixtures import (
    _PROPERTY_SEEDS,
    _assert_zero_residual,
    _random_additive,
    _random_multiplicative,
    _random_ratio,
    _random_structural,
)


@pytest.mark.parametrize("seed", range(_PROPERTY_SEEDS))
def test_zero_residual_property(seed: int) -> None:
    """四种分解在随机输入上都必须零残差(固定种子 → 失败可复现)。"""
    rng = random.Random(seed)

    base, cmp_, factors_base, factors_cmp = _random_multiplicative(rng)
    _assert_zero_residual(decompose_multiplicative(base, cmp_, factors_base, factors_cmp), base, cmp_)

    base, cmp_, parts_base, parts_cmp = _random_additive(rng)
    _assert_zero_residual(decompose_additive(base, cmp_, parts_base, parts_cmp), base, cmp_)

    n_base, n_cmp, d_base, d_cmp = _random_ratio(rng)
    _assert_zero_residual(
        decompose_ratio(n_base, n_cmp, d_base, d_cmp), n_base / d_base, n_cmp / d_cmp
    )

    base, cmp_, w_base, w_cmp, r_base, r_cmp = _random_structural(rng)
    _assert_zero_residual(
        decompose_structural(base, cmp_, w_base, w_cmp, r_base, r_cmp), base, cmp_
    )
