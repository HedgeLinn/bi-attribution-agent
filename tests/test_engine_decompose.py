"""AttributionEngine.decompose 的集成测试:真实数据集上跑通「声明 -> 取数 -> LMDI -> 整形」。

纯函数的单元测试见 test_decompose_*.py;这里验证的是**编排**:声明怎么驱动取数、
切片从哪来、返回键与数值整形是否符合 AttributionEngine.decompose 的冻结契约。
需要「换一张地图」的用例(真实地图没声明的 additive / ratio)写临时语义层到 tmp_path,
指向同一份真实 parquet —— 夹具见 tests/engine_fixtures.py。

真实数据的埋点异常是归因的「标准答案」:华东 -> 上海 -> STORE_S0001(上海徐家汇旗舰店)
自 2026-06 断崖下跌,根因是头部 SKU 下架 -> 该店客单价腰斩。
"""

import pytest

from attribution.decompose import DecomposeError
from attribution.semantic import SemanticError
from tests.engine_fixtures import (
    BASE_WINDOW,
    CMP_WINDOW,
    STRUCTURAL_ARITY_LAYER_YAML,
    engine_with_layer,
    real_engine,
)

# 契约键(AttributionEngine.decompose 的 docstring):多一个少一个都算漂移
_WHOLE_KEYS = {"target", "kind", "factors", "dimension", "level",
               "total_base", "total_cmp", "total_change", "effects"}
_SLICE_KEYS = {"key", "label", "total_base", "total_cmp", "total_change", "effects"}
_EFFECT_KEYS = {"factor", "label", "base", "cmp", "effect", "contribution", "change_rate"}
# 结构分解额外单列「下架/新上」实体(任一期分母为 0),整窗与切片都带 entity_changes
_STRUCTURAL_WHOLE_KEYS = _WHOLE_KEYS | {"entity_changes"}
_STRUCTURAL_SLICE_KEYS = _SLICE_KEYS | {"entity_changes"}

_TOLERANCE = 1e-6


def _sum_effects(entry: dict) -> float:
    """Σ effect —— 零残差断言的对象(与纯函数层同一个恒等式)。"""
    return sum(effect["effect"] for effect in entry["effects"])


def _assert_identity(entry: dict) -> None:
    """Σ effect ≡ total_change(相对容差):分解的数必须收得回来。"""
    assert _sum_effects(entry) == pytest.approx(
        entry["total_change"], rel=_TOLERANCE, abs=_TOLERANCE
    )


def test_multiplicative_whole_window_identity_on_real_data() -> None:
    """整窗乘法分解:GMV = 客单价 × 订单数,零残差,键与 label 全部合契约。"""
    engine = real_engine()
    result = engine.decompose("gmv", ["aov", "orders_count"], *BASE_WINDOW, *CMP_WINDOW)

    assert set(result) == _WHOLE_KEYS              # 不带 dimension/level -> 没有 slices
    assert result["target"] == "gmv"
    assert result["kind"] == "multiplicative"
    assert result["factors"] == ["aov", "orders_count"]
    assert result["dimension"] is None and result["level"] is None
    # 总量与「直接查这个指标」同源:分解不许自己另算一套口径
    assert result["total_base"] == engine.query_metric("gmv", [], {}, *BASE_WINDOW)["total"]
    assert result["total_cmp"] == engine.query_metric("gmv", [], {}, *CMP_WINDOW)["total"]
    assert result["total_change"] == pytest.approx(
        result["total_cmp"] - result["total_base"], rel=_TOLERANCE, abs=_TOLERANCE)
    assert result["total_change"] < 0
    _assert_identity(result)
    # label 来自语义层,而不是指标名本身
    assert [e["label"] for e in result["effects"]] == ["客单价", "订单数"]
    assert all(set(e) == _EFFECT_KEYS for e in result["effects"])


def test_multiplicative_slices_locate_the_buried_anomaly() -> None:
    """带 dimension/level 的切片分解:必须下钻到门店层,头名就是埋点的那家店。

    埋点异常的标准答案:上海徐家汇旗舰店自 2026-06 断崖下跌,主因是客单价腰斩。
    若引擎停在「整体下跌」而没落到门店/SKU 层,归因就没有完成。
    """
    result = real_engine().decompose(
        "gmv", ["aov", "orders_count"], *BASE_WINDOW, *CMP_WINDOW,
        dimension="store", level="store_id", top_k=3)

    assert set(result) == _WHOLE_KEYS | {"slices"}
    assert (result["dimension"], result["level"]) == ("store", "store_id")
    assert len(result["slices"]) == 3              # top_k 截断生效

    top = result["slices"][0]
    assert set(top) == _SLICE_KEYS
    assert top["key"] == "STORE_S0001"
    assert top["label"] == "上海徐家汇旗舰店"      # key 层用 name_column 展示,不是 ID
    assert top["total_change"] < 0
    assert top["effects"][0]["factor"] == "aov"
    assert top["effects"][0]["change_rate"] < -0.4   # 客单价腰斩(约 −0.50)

    changes = [abs(slice_["total_change"]) for slice_ in result["slices"]]
    assert changes == sorted(changes, reverse=True)  # 切片按 |变化量| 降序
    for slice_ in result["slices"]:
        _assert_identity(slice_)


def test_slice_totals_match_contribute_on_real_data() -> None:
    """切片口径与 contribute 一致:同一 level 下 key / label / base / cmp 逐值相同。

    两条路径各自取数,如果对不上,说明「分解的切片」与「下钻的切片」是两套口径,
    归因结论会自相矛盾。
    """
    engine = real_engine()
    decomposed = engine.decompose("gmv", ["aov", "orders_count"], *BASE_WINDOW, *CMP_WINDOW,
                                  dimension="store", level="store_id", top_k=500)
    drilled = engine.contribute("gmv", "store", "store_id", *BASE_WINDOW, *CMP_WINDOW, top_k=500)

    from_decompose = {s["key"]: (s["label"], s["total_base"], s["total_cmp"])
                      for s in decomposed["slices"]}
    from_contribute = {t["key"]: (t["label"], t["base"], t["cmp"]) for t in drilled["top"]}
    assert from_decompose == from_contribute
    assert (decomposed["total_base"], decomposed["total_cmp"]) == (
        drilled["total_base"], drilled["total_cmp"])


def test_structural_whole_window_identity_on_real_data() -> None:
    """结构分解:客单价 = Σ(商品销量占比 × 商品自身单价),两条效应收得回总变化。

    结构效应的 base / cmp / change_rate 恒为 None(它对应的是聚合量,没有单一观测值),
    这是纯函数层的既定口径,不是缺字段。下架/新上实体(任一期分母为 0)单列进
    entity_changes,不并入 effects(保持两条效应的零残差恒等式)。
    """
    result = real_engine().decompose("aov", ["product_mix", "price"], *BASE_WINDOW, *CMP_WINDOW)

    assert set(result) == _STRUCTURAL_WHOLE_KEYS
    assert result["kind"] == "structural"
    assert result["factors"] == ["product_mix", "price"]
    assert [e["factor"] for e in result["effects"]] == ["__mix__", "__rate__"]
    assert [e["label"] for e in result["effects"]] == ["结构效应", "自身效应"]
    assert all(e["base"] is None and e["cmp"] is None and e["change_rate"] is None
               for e in result["effects"])
    _assert_identity(result)


def test_structural_entity_changes_surfaces_delisted_sku() -> None:
    """结构分解的 entity_changes:下架 SKU 单列,不因分母为 0 而消失。

    埋点根因 = 头部 SKU SKU_P0001 自 2026-06 下架。它对比期分母(销量)为 0,
    不进入 LMDI 效应,但必须记进 entity_changes 且 only_in == "base"——这正是
    R2 缺陷的回归锚点:引擎不能再把它悄悄丢掉。
    """
    result = real_engine().decompose("aov", ["product_mix", "price"], *BASE_WINDOW, *CMP_WINDOW)

    entries = {e["entity"]: e for e in result["entity_changes"]}
    assert "SKU_P0001" in entries
    delisted = entries["SKU_P0001"]
    assert delisted["only_in"] == "base"
    assert delisted["label"] == "数码家电手机通讯-001号"   # key 层用 name_column 展示
    assert delisted["denominator_base"] > 0
    assert delisted["denominator_cmp"] == 0.0              # 下架 = 对比期销量为 0
    # 两侧分母都为正的实体不进入 entity_changes(它们已在 LMDI 效应里)
    assert all(e["denominator_base"] == 0.0 or e["denominator_cmp"] == 0.0
               for e in result["entity_changes"])


def test_structural_slices_identity_on_real_data() -> None:
    """结构分解的切片形态:每个门店各分解一次,逐切片零残差,且各带自己的 entity_changes。"""
    result = real_engine().decompose(
        "aov", ["product_mix", "price"], *BASE_WINDOW, *CMP_WINDOW,
        dimension="store", level="store_id", top_k=3)

    assert len(result["slices"]) == 3
    assert all(set(slice_) == _STRUCTURAL_SLICE_KEYS for slice_ in result["slices"])
    assert all(slice_["key"] and slice_["label"] for slice_ in result["slices"])
    for slice_ in result["slices"]:
        assert [e["factor"] for e in slice_["effects"]] == ["__mix__", "__rate__"]
        _assert_identity(slice_)
        # 每个切片都带自己的 entity_changes(下架 SKU 在该门店内同样单列)
        assert isinstance(slice_["entity_changes"], list)


def test_additive_identity_via_temp_layer(tmp_path) -> None:
    """加法分解(临时地图):毛额 = 净额 + 优惠额,Σ效应 ≡ 总变化。"""
    engine = engine_with_layer(tmp_path)
    result = engine.decompose("gross", ["net", "discount_amount"], *BASE_WINDOW, *CMP_WINDOW)

    assert result["kind"] == "additive"
    assert [e["label"] for e in result["effects"]] == ["净额", "优惠额"]
    assert result["total_base"] == engine.query_metric("gross", [], {}, *BASE_WINDOW)["total"]
    _assert_identity(result)
    # 加法分解的效应就是各分项自己的变化量
    for effect in result["effects"]:
        assert effect["effect"] == pytest.approx(
            effect["cmp"] - effect["base"], rel=_TOLERANCE, abs=_TOLERANCE)


def test_ratio_identity_via_temp_layer(tmp_path) -> None:
    """比率分解(临时地图):客单价 = 毛额 / 订单数;整窗与切片都零残差。"""
    engine = engine_with_layer(tmp_path)
    result = engine.decompose("unit_price", ["gross", "orders_cnt"], *BASE_WINDOW, *CMP_WINDOW)

    assert result["kind"] == "ratio"
    assert result["factors"] == ["gross", "orders_cnt"]     # 声明顺序 = [分子, 分母]
    assert [e["factor"] for e in result["effects"]] == ["numerator", "denominator"]
    assert [e["label"] for e in result["effects"]] == ["分子效应", "分母效应"]
    _assert_identity(result)

    sliced = engine.decompose("unit_price", ["gross", "orders_cnt"], *BASE_WINDOW, *CMP_WINDOW,
                              dimension="store", level="city", top_k=2)
    assert len(sliced["slices"]) == 2
    for slice_ in sliced["slices"]:
        _assert_identity(slice_)
        assert slice_["key"] and slice_["label"]


def test_ratio_slices_with_zero_denominator_are_skipped(tmp_path) -> None:
    """比率切片:分母为 0 的切片直接跳过(不参与也不记),而不是编一个数出来。

    同一个「基期只有部分门店营业」的窗口:乘法分解会为 445 个门店出切片(缺的一侧按 0),
    比率分解只剩 352 个 —— 差额正是基期没有订单(分母为 0)的门店:比率在那里无定义。
    """
    windows = ("2024-09-01", "2024-09-02", "2026-06-01", "2026-06-02")
    ratio = engine_with_layer(tmp_path).decompose(
        "unit_price", ["gross", "orders_cnt"], *windows,
        dimension="store", level="store_id", top_k=1000)
    multiplicative = real_engine().decompose(
        "gmv", ["aov", "orders_count"], *windows,
        dimension="store", level="store_id", top_k=1000)

    assert ratio["slices"] and len(ratio["slices"]) < len(multiplicative["slices"])
    assert not [s for s in ratio["slices"] if s["total_base"] == 0.0 or s["total_cmp"] == 0.0]
    for slice_ in ratio["slices"]:
        denominator = next(e for e in slice_["effects"] if e["factor"] == "denominator")
        assert denominator["base"] > 0 and denominator["cmp"] > 0
        _assert_identity(slice_)


def test_structural_target_must_be_ratio_shaped(tmp_path) -> None:
    """structural 的泛化规则:target 必须是「分子 / 分母」形(依赖恰两个),否则 ValueError。

    这条规则不认任何具体指标名——它只看依赖个数与顺序,所以「客单价 = GMV / 订单数」
    与其它任何比率指标走的是同一条路。
    """
    engine = engine_with_layer(tmp_path, STRUCTURAL_ARITY_LAYER_YAML)
    with pytest.raises(ValueError, match="分子"):
        engine.decompose("gross", ["product_mix", "price"], *BASE_WINDOW, *CMP_WINDOW)


def test_undeclared_decomposition_raises_semantic_error() -> None:
    """没声明过的分解不许现编:命中不到声明一律 SemanticError。"""
    engine = real_engine()
    with pytest.raises(SemanticError):
        engine.decompose("gmv", ["aov"], *BASE_WINDOW, *CMP_WINDOW)       # 因子集合不符
    with pytest.raises(SemanticError):
        engine.decompose("channel", ["aov", "orders_count"], *BASE_WINDOW, *CMP_WINDOW)
    # 错误信息要列出该 target 已声明的组合,否则调用方不知道该怎么改
    with pytest.raises(SemanticError, match="已声明的组合"):
        engine.decompose("gmv", ["aov"], *BASE_WINDOW, *CMP_WINDOW)


def test_level_outside_hierarchy_raises_value_error() -> None:
    """level 必须是该维度 hierarchy 里的字段,否则 ValueError(与 contribute 同口径)。"""
    engine = real_engine()
    with pytest.raises(ValueError):
        engine.decompose("gmv", ["aov", "orders_count"], *BASE_WINDOW, *CMP_WINDOW,
                         dimension="store", level="store")
    with pytest.raises(SemanticError):
        engine.decompose("gmv", ["aov", "orders_count"], *BASE_WINDOW, *CMP_WINDOW,
                         dimension="查无此维", level="store_id")


def test_one_sided_slice_falls_back_to_zero() -> None:
    """只在单侧窗口出现的切片:缺失一侧按 0 参与,并由纯函数层标注「近似」。

    基期取数据集最早两天(并非所有门店都已开店),对比期取 2026-06 —— 于是在基期
    没有数据的门店就是「单侧切片」。按 0 处理而不是丢切片:门店数变化本身也是信息。
    """
    result = real_engine().decompose(
        "gmv", ["aov", "orders_count"], "2024-09-01", "2024-09-02", *CMP_WINDOW,
        dimension="store", level="store_id", top_k=1000)

    one_sided = [s for s in result["slices"] if s["total_base"] == 0.0]
    assert one_sided, "该窗口下应当存在只在对比期出现过的门店"
    for slice_ in one_sided:
        assert slice_["total_cmp"] > 0
        for effect in slice_["effects"]:
            assert effect["base"] == 0.0
            assert "近似" in effect["label"]      # δ 替代必须自报家门
            assert effect["change_rate"] is None  # base 为 0 -> 不除零


def test_filters_scope_target_and_factors() -> None:
    """filters 同时作用于 target 与全部因子:分解的数与同条件下直接查的值逐值相同。"""
    engine = real_engine()
    filters = {"region": "华东"}
    result = engine.decompose("gmv", ["aov", "orders_count"], *BASE_WINDOW, *CMP_WINDOW,
                              filters=filters)

    assert result["total_base"] == engine.query_metric(
        "gmv", [], filters, *BASE_WINDOW)["total"]
    # 因子取值同样带过滤:两个因子的基期值各自与「同条件直查」相同
    aov_effect, orders_effect = result["effects"]
    assert aov_effect["base"] == engine.query_metric("aov", [], filters, *BASE_WINDOW)["total"]
    assert orders_effect["base"] == engine.query_metric(
        "orders_count", [], filters, *BASE_WINDOW)["total"]
    # 过滤后总量确实变小(全量约 1369 万,华东区约 532 万)
    assert result["total_base"] < engine.query_metric("gmv", [], {}, *BASE_WINDOW)["total"]


def test_window_without_data_raises_decompose_error() -> None:
    """窗口内没有任何数据时如实报错:没有行不等于 0,不许拿 0 冒充基期。"""
    with pytest.raises(DecomposeError, match="没有任何数据"):
        real_engine().decompose("gmv", ["aov", "orders_count"],
                                "2023-01-01", "2023-01-31", *CMP_WINDOW)
