"""四种分解(§4.2):乘法 / 加法 / 比率 / 结构。

核心是 **LMDI(对数平均迪氏指数)**,它相对链式替代法的关键性质是**完美可加、零残差**:

    ΔV = V_t − V_0
    效应_i = L(V_t, V_0) · ln(f_i,t / f_i,0)
    其中 L(a, b) = (a − b) / (ln a − ln b)      对数平均

    Σ 效应_i = L · ln(Π f_i,t / f_i,0) = L · ln(V_t / V_0) = V_t − V_0   ✓

交互项被自动吸收,且**与因子顺序无关**(链式替代法两者都不满足)。

**本模块是纯函数**:输入数值、输出效应列表。不碰 DuckDB、不碰 Semantic、不做 IO。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import log, prod

__all__ = [
    "DecomposeError",
    "Effect",
    "KIND_ADDITIVE",
    "KIND_MULTIPLICATIVE",
    "KIND_RATIO",
    "KIND_STRUCTURAL",
    "decompose_additive",
    "decompose_multiplicative",
    "decompose_ratio",
    "decompose_structural",
]

# 分解类型标识,与语义层 decompositions.kind 的取值一一对应(§3.2)
KIND_MULTIPLICATIVE = "multiplicative"
KIND_ADDITIVE = "additive"
KIND_RATIO = "ratio"
KIND_STRUCTURAL = "structural"

# ---- 边界与键名常量:一律命名,不接受魔法数字 ----
_MIN_TOTAL_CHANGE = 1e-12      # 总变化小于此值即视为「未变化」:对数平均分母为 0,占比亦无从计算
_DELTA_SUBSTITUTE = 1e-12      # 非正因子的 δ 替代量(对数无定义;替代后结果为近似,须标注)
_IDENTITY_TOLERANCE = 1e-6     # 恒等式 V = Π f / Σ p / Σ w·r 的相对容差:超出即判为调用方口径错误
_WEIGHT_SUM_TOLERANCE = 1e-6   # 权重是占比,其和与 1 的容差
_APPROX_SUFFIX = "(含非正值,效应为近似)"     # δ 替代后追加到 label:调用方据此提示「效应为近似」
_FACTOR_MIX = "__mix__"        # 结构分解两条汇总项的键名(docstring 已冻结其取值)
_FACTOR_RATE = "__rate__"
_FACTOR_NUMERATOR = "numerator"      # 比率分解两条因子:V = N / D = N · D⁻¹
_FACTOR_DENOMINATOR = "denominator"
_DEFAULT_LABELS = {            # 缺省展示名:免得把内部键名露到界面上
    _FACTOR_MIX: "结构效应", _FACTOR_RATE: "自身效应",
    _FACTOR_NUMERATOR: "分子效应", _FACTOR_DENOMINATOR: "分母效应",
}


class DecomposeError(Exception):
    """分解无法进行(输入长度不匹配、权重不合法等)。"""


@dataclass(frozen=True)
class Effect:
    """一个因子的分解结果。

    `effect` 是**绝对量**,所有因子的 effect 之和恒等于 `total_change`(零残差)。
    `contribution` 是它在总变化里的占比;`total_change` 为 0 时置 None(不做除零)。
    """

    factor: str
    label: str
    effect: float
    contribution: float | None = None
    change_rate: float | None = None
    base: float | None = None
    cmp: float | None = None


def decompose_multiplicative(
    total_base: float,
    total_cmp: float,
    factors_base: Mapping[str, float],
    factors_cmp: Mapping[str, float],
    labels: Mapping[str, str] | None = None,
) -> list[Effect]:
    """乘法分解:`target = Π factors`(如 量价分解)。

    每个因子一条 Effect,`Σ effect ≡ total_cmp − total_base`。

    边界(必须处理,否则会静默出错):
        - 因子值为 **0 或负**:对数无定义。用极小量 δ 替代后计算,并在该因子的
          label 后缀标注(调用方据此提示"该因子含非正值,效应为近似")。
        - `total_cmp == total_base`:对数平均的分母为 0,所有 effect 置 0。
        - 某因子未变化(`f_t == f_0`):该项效应自然为 0,不需特判。
        - 因子集合不一致:抛 `DecomposeError`。
    """
    keys = _matched_keys(factors_base, factors_cmp, "乘法分解因子")
    raw = {key: (factors_base[key], factors_cmp[key]) for key in keys}
    pairs = {key: (_substitute(values[0]), _substitute(values[1])) for key, values in raw.items()}
    total_change = total_cmp - total_base
    # 契约:V_t == V_0 时对数平均的分母为 0 → 全部置 0(不除零,也不编方向)
    if abs(total_change) <= _MIN_TOTAL_CHANGE:
        return [_build(key, labels, 0.0, total_change, raw[key]) for key in keys]
    # 非正值取对数无定义:逐因子用 δ 替代,替代过的因子由 label 后缀标注「近似」
    approx = {key: pairs[key] != raw[key] for key in keys}
    rebuilt = (prod(pair[0] for pair in pairs.values()), prod(pair[1] for pair in pairs.values()))
    if any(approx.values()):
        basis_base, basis_cmp = rebuilt      # δ 已改写乘积 V = Π f:基准量只能取替代后的重建量
    else:
        # 恒等式 V = Π f 是零残差的前提,不成立时 Σ effect 必然对不上 ΔV → fail fast
        _require_identity(total_base, rebuilt[0], "基期")
        _require_identity(total_cmp, rebuilt[1], "对比期")
        basis_base, basis_cmp = total_base, total_cmp
    # 所有因子共用同一个 L(V_t, V_0):交互项被自动吸收,结果与因子顺序无关
    log_mean = _log_mean(basis_cmp, basis_base)
    return [
        _build(key, labels, log_mean * _log_ratio(pairs[key]), total_change, raw[key], approx[key])
        for key in keys
    ]


def decompose_additive(
    total_base: float,
    total_cmp: float,
    parts_base: Mapping[str, float],
    parts_cmp: Mapping[str, float],
    labels: Mapping[str, str] | None = None,
) -> list[Effect]:
    """加法分解:`target = Σ parts`(如 MRR waterfall:新增 + 扩张 − 收缩 − 流失)。

    加法分解无需 LMDI——每个部分的 `effect` 就是它自己的变化量,天然零残差。
    保留同样返回结构,是为了让上层用统一方式消费。

    注意 `parts` 里**允许负值**(收缩、流失通常记负)。
    """
    keys = _matched_keys(parts_base, parts_cmp, "加法分解分项")
    raw = {key: (parts_base[key], parts_cmp[key]) for key in keys}
    # 零残差的前提同样是恒等式 Σ p = V:分项没覆盖目标(或口径不符)时报错,不返回带残差的数
    _require_identity(total_base, sum(value[0] for value in raw.values()), "基期")
    _require_identity(total_cmp, sum(value[1] for value in raw.values()), "对比期")
    total_change = total_cmp - total_base
    # 加法分解不需要 LMDI:每个分项的效应就是它自己的变化量(允许负值),天然零残差
    return [_build(key, labels, raw[key][1] - raw[key][0], total_change, raw[key]) for key in keys]


def decompose_ratio(
    numerator_base: float,
    numerator_cmp: float,
    denominator_base: float,
    denominator_cmp: float,
) -> list[Effect]:
    """比率分解:`target = 分子 / 分母`,拆成「分子效应」与「分母效应」两条。

    实现上等价于对 `[分子, 1/分母]` 做乘法分解——因为 `N/D = N · D^-1`:

        分子效应 = L(V_t, V_0) · ln(N_t / N_0)
        分母效应 = L(V_t, V_0) · ln(D_0 / D_t)

    于是「转化率下降了多少来自分子、多少来自分母」可被量化。

    边界:分母为 0 时抛 `DecomposeError`(比率本身无定义,不该由本函数编一个数)。
    """
    if denominator_base == 0 or denominator_cmp == 0:
        raise DecomposeError("分母为 0:比率本身无定义,不由本函数编一个数出来")
    # 分母因子取倒数,故其效应为 L · ln(D_0 / D_t);非正的分子 / 分母同样走 δ 替代
    nums = (_substitute(numerator_base), _substitute(numerator_cmp))
    dens = (_substitute(denominator_base), _substitute(denominator_cmp))
    approx = nums != (numerator_base, numerator_cmp) or dens != (denominator_base, denominator_cmp)
    ratio_base, ratio_cmp = nums[0] / dens[0], nums[1] / dens[1]      # V_0 = N_0/D_0,V_t = N_t/D_t
    total_change = ratio_cmp - ratio_base
    log_mean = 0.0 if abs(total_change) <= _MIN_TOTAL_CHANGE else _log_mean(ratio_cmp, ratio_base)
    num_effect = log_mean * (log(nums[1]) - log(nums[0]))            # ln(N_t / N_0)
    den_effect = log_mean * (log(dens[0]) - log(dens[1]))            # ln(D_0 / D_t)
    return [
        _build(_FACTOR_NUMERATOR, None, num_effect, total_change, (numerator_base, numerator_cmp), approx),
        _build(_FACTOR_DENOMINATOR, None, den_effect, total_change, (denominator_base, denominator_cmp), approx),
    ]


def decompose_structural(
    total_base: float,
    total_cmp: float,
    weights_base: Mapping[str, float],
    weights_cmp: Mapping[str, float],
    rates_base: Mapping[str, float],
    rates_cmp: Mapping[str, float],
    labels: Mapping[str, str] | None = None,
) -> list[Effect]:
    """结构分解(§4.3,两层 LMDI):`V = Σ_i (w_i × r_i)`,拆成**结构效应**与**自身效应**。

    返回两条 Effect(而非每个切片一条):`__mix__`(结构)与 `__rate__`(自身),
    外加各切片明细时由调用方决定——本函数只保证两条之和 ≡ `total_cmp − total_base`。

        结构效应 = Σ_i L(V_i,t, V_i,0) · ln(w_i,t / w_i,0)
        自身效应 = Σ_i L(V_i,t, V_i,0) · ln(r_i,t / r_i,0)
        其中 V_i = w_i × r_i

    这是回答「整体下降是**普遍变化**还是**结构变化**」的唯一办法:
    例如整体均值下滑,可能全部来自高权重切片退出(结构效应),而各切片自身都没变。

    边界:`weights_*` 各切片之和应为 1(权重占比);偏差过大抛 `DecomposeError`
    ——否则 `V = Σ w_i r_i` 不成立,分解出的数是错的。
    """
    keys = _matched_keys(weights_base, weights_cmp, "权重切片")
    rate_keys = _matched_keys(rates_base, rates_cmp, "强度切片")
    if set(rate_keys) != set(keys):
        raise DecomposeError(f"权重与强度的切片集合不一致:权重 {keys},强度 {rate_keys}")
    _require_unit_weights(weights_base, "基期")
    _require_unit_weights(weights_cmp, "对比期")

    # 权重与强度各自 δ 替代:两条效应的对数比之和必须正好等于 ln(V_i,t/V_i,0),故 L 用到的
    # V_i 只能由替代后的 w、r 相乘得到(否则零残差失守);非正值由 label 后缀标注「近似」
    raw = {key: (weights_base[key], weights_cmp[key], rates_base[key], rates_cmp[key]) for key in keys}
    slices = {key: tuple(map(_substitute, values)) for key, values in raw.items()}
    approx = any(slices[key] != raw[key] for key in keys)
    if not approx:
        # 恒等式 V = Σ w·r 是零残差的前提;δ 替代会改写它,那时以替代后的重建量为准
        _require_identity(total_base, sum(value[0] * value[2] for value in raw.values()), "基期")
        _require_identity(total_cmp, sum(value[1] * value[3] for value in raw.values()), "对比期")

    mix, rate = _structural_terms(slices)
    total_change = total_cmp - total_base
    return [
        _build(_FACTOR_MIX, labels, mix, total_change, (None, None), approx),
        _build(_FACTOR_RATE, labels, rate, total_change, (None, None), approx),
    ]


# ------------------------- 内部辅助(不对外暴露)-------------------------
# 零残差不是巧合:效应都是「同一个 L × 该因子自己的对数比」,没有交互项、没有事后修正项、
# 不做任何四舍五入;代入恒等式 V = Π f 即得 Σ effect = L · ln(V_t/V_0) = V_t − V_0。
# 恒等式在代码里被显式校验,前提不成立时宁可报错,也不返回带残差的数。
def _log_mean(a: float, b: float) -> float:
    """对数平均 L(a, b) = (a − b) / (ln a − ln b);a == b 时取极限 L(a, a) = a。"""
    return a if a == b else (a - b) / (log(a) - log(b))


def _log_ratio(pair: tuple[float, float]) -> float:
    """ln(f_t / f_0)(pair 内的值已由 _substitute 保证为正);用对数之差,不比值,不会溢出。"""
    return log(pair[1]) - log(pair[0])


def _substitute(value: float) -> float:
    """非正值取对数无定义:用极小量 δ 替代(结果降级为近似,由 label 后缀声明)。"""
    return value if value > 0 else _DELTA_SUBSTITUTE


def _matched_keys(base: Mapping[str, float], cmp: Mapping[str, float], what: str) -> list[str]:
    """因子集合两期必须一致:只在一侧出现的因子无从配对,分解不出它的效应。"""
    only_base, only_cmp = [k for k in base if k not in cmp], [k for k in cmp if k not in base]
    if only_base or only_cmp:
        raise DecomposeError(f"{what}集合不一致:仅基期有 {only_base},仅对比期有 {only_cmp}")
    return list(base)


def _require_identity(given: float, rebuilt: float, period: str) -> None:
    """校验恒等式(相对容差):它是零残差的前提,不成立时报错,不给近似数。"""
    if abs(given - rebuilt) > _IDENTITY_TOLERANCE * max(abs(given), abs(rebuilt)):
        raise DecomposeError(
            f"{period}总量与各因子重建值不符:{given!r} vs {rebuilt!r}——恒等式不成立时,"
            "效应之和不会等于总变化(检查口径或语义层的分解声明)"
        )


def _require_unit_weights(weights: Mapping[str, float], period: str) -> None:
    """权重是占比,和必须为 1——否则 V = Σ w·r 不成立,分解出的数是错的。"""
    total = sum(weights.values())
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise DecomposeError(f"{period}权重之和应为 1,实际为 {total}(权重口径不是占比?)")


def _structural_terms(slices: Mapping[str, Sequence[float]]) -> tuple[float, float]:
    """逐切片累加两层 LMDI,返回 (结构效应, 自身效应):两者之和 = Σ_i (V_i,t − V_i,0) = V_t − V_0。"""
    mix = 0.0
    rate = 0.0
    for w_base, w_cmp, r_base, r_cmp in slices.values():
        log_mean = _log_mean(w_cmp * r_cmp, w_base * r_base)     # 每个切片只有一条 L(V_i,t, V_i,0)
        mix += log_mean * (log(w_cmp) - log(w_base))
        rate += log_mean * (log(r_cmp) - log(r_base))
    return mix, rate


def _build(
    factor: str,
    labels: Mapping[str, str] | None,
    effect: float,
    total_change: float,
    values: tuple[float | None, float | None],
    approx: bool = False,
) -> Effect:
    """统一构造 Effect:label 缺省查缺省展示名,δ 替代过的因子追加近似标注;
    `values` = (基期取值, 对比期取值) 报原始值(观测量),效应才是近似量。"""
    label = (labels or {}).get(factor) or _DEFAULT_LABELS.get(factor) or factor
    base, cmp = values
    return Effect(
        factor=factor,
        label=label + _APPROX_SUFFIX if approx else label,
        effect=effect,
        contribution=None if abs(total_change) <= _MIN_TOTAL_CHANGE else effect / total_change,
        change_rate=(cmp - base) / base if base and cmp is not None else None,
        base=base,
        cmp=cmp,
    )
