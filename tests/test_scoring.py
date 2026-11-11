"""`engine/scoring.py` 的测试（任务 M2-2）。

验收要求「与 §4.2 锚点表一致」—— 因此本文件把**《评分算法规格》§4.2 表格的数值直接硬编码**
（而不是从 config 读），再与 `config.ABS_ANCHORS` + `interp_anchor` 对照。
这样一旦谁改错了锚点，测试会指出来（config 自身的自检在 test_config.py，两者互为交叉验证）。
"""

from __future__ import annotations

import json

import pytest

from src import config
from src.engine import indicators, scoring
from src.engine.scoring import (
    absolute_sub_score,
    absolute_sub_scores,
    cagr,
    growth_from_rows,
    growth_item,
    growth_score,
    interp_anchor,
    stability_k,
)

# 《评分算法规格》§4.2 方式一表格（0 分 / 50 分 / 80 分 / 100 分；比率用小数）
SPEC_ANCHORS: dict[str, tuple[float, float, float, float]] = {
    "roe": (0.00, 0.10, 0.20, 0.35),
    "gross_margin": (0.00, 0.20, 0.30, 0.40),
    "net_margin": (0.00, 0.10, 0.20, 0.30),
    "cash_ratio": (0.00, 0.10, 0.25, 0.35),
    "roa": (0.00, 0.05, 0.10, 0.15),
}
SPEC_SCORES = (0.0, 50.0, 80.0, 100.0)


# ── 与 §4.2 锚点表逐项一致（验收核心）────────────────────────────────────
def test_absolute_indicator_keys_match_spec_table() -> None:
    assert set(scoring.ABS_INDICATOR_KEYS) == set(SPEC_ANCHORS)
    assert set(config.ABS_ANCHORS) == set(SPEC_ANCHORS)


@pytest.mark.parametrize("key", sorted(SPEC_ANCHORS))
def test_config_anchors_equal_spec_table(key: str) -> None:
    """config 的锚点必须逐点等于 §4.2 表格（四档 x 与 y 都对齐）。"""
    expected = list(zip(SPEC_ANCHORS[key], SPEC_SCORES, strict=True))
    assert config.ABS_ANCHORS[key] == expected


@pytest.mark.parametrize("key", sorted(SPEC_ANCHORS))
@pytest.mark.parametrize("index", range(4))
def test_anchor_points_map_to_their_scores(key: str, index: int) -> None:
    """落在锚点上 → 恰好得到该档分数（0 / 50 / 80 / 100）。"""
    x = SPEC_ANCHORS[key][index]
    assert interp_anchor(x, config.ABS_ANCHORS[key]) == SPEC_SCORES[index]


@pytest.mark.parametrize("key", sorted(SPEC_ANCHORS))
def test_segment_midpoints_are_linear(key: str) -> None:
    """段内取中点 → 两个锚点分数的中点（验证 50→80 与 80→100 两段都是线性的）。"""
    x0, x50, x80, x100 = SPEC_ANCHORS[key]
    anchors = config.ABS_ANCHORS[key]
    assert interp_anchor((x0 + x50) / 2, anchors) == pytest.approx(25.0)
    assert interp_anchor((x50 + x80) / 2, anchors) == pytest.approx(65.0)
    assert interp_anchor((x80 + x100) / 2, anchors) == pytest.approx(90.0)


# ── 截断：越界与负值 ──────────────────────────────────────────────────────
def test_below_first_anchor_clamps_to_zero_including_negative() -> None:
    anchors = config.ABS_ANCHORS["roe"]
    assert interp_anchor(0.0, anchors) == 0.0
    assert interp_anchor(-0.01, anchors) == 0.0
    assert interp_anchor(-5.0, anchors) == 0.0  # 净资产为负 → roe 很负 → 0 分


def test_above_last_anchor_clamps_to_hundred() -> None:
    anchors = config.ABS_ANCHORS["roe"]
    assert interp_anchor(0.35, anchors) == 100.0
    assert interp_anchor(0.80, anchors) == 100.0


@pytest.mark.parametrize("key", sorted(SPEC_ANCHORS))
def test_sub_score_never_leaves_zero_hundred(key: str) -> None:
    anchors = config.ABS_ANCHORS[key]
    for value in (-1.0, -0.001, 0.0, 0.001, 0.5, 1.0, 10.0):
        assert config.SUB_SCORE_MIN <= interp_anchor(value, anchors) <= config.SUB_SCORE_MAX


def test_scores_are_monotonic_non_decreasing() -> None:
    """单调不减：指标值变大，子分不能变小（插值实现写反会立刻被抓）。"""
    anchors = config.ABS_ANCHORS["cash_ratio"]
    values = [index / 100 for index in range(-20, 60)]
    scores = [interp_anchor(value, anchors) for value in values]
    assert scores == sorted(scores)


def test_continuity_across_anchor_boundaries() -> None:
    """锚点两侧不跳变（左极限 == 该锚点分数）。"""
    anchors = config.ABS_ANCHORS["gross_margin"]
    for x_anchor, y_anchor in anchors[1:]:
        left = interp_anchor(x_anchor - 1e-9, anchors)
        assert left == pytest.approx(y_anchor, abs=1e-6)


# ── 参数校验（不静默容忍配置错误）────────────────────────────────────────
def test_empty_anchors_raise() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        interp_anchor(0.1, [])


def test_unsorted_anchors_raise_instead_of_silently_sorting() -> None:
    with pytest.raises(ValueError, match="升序"):
        interp_anchor(0.1, [(0.0, 0.0), (0.30, 80.0), (0.20, 50.0)])


def test_duplicate_anchor_x_does_not_divide_by_zero() -> None:
    assert interp_anchor(0.2, [(0.0, 0.0), (0.2, 50.0), (0.2, 80.0), (0.4, 100.0)]) == 50.0


def test_unknown_indicator_key_raises() -> None:
    with pytest.raises(KeyError, match="不是绝对值锚点指标"):
        absolute_sub_score("net_profit_scale", 10.0)  # 分位类，不归本模块


# ── 组装 5 项绝对值子分 ───────────────────────────────────────────────────
def test_absolute_sub_scores_cover_exactly_the_five_absolute_keys() -> None:
    result = absolute_sub_scores({"roe": 0.20, "roa": 0.10})
    assert set(result.values) == set(config.ABS_ANCHORS)
    assert result.keys == scoring.ABS_INDICATOR_KEYS


def test_absolute_sub_scores_values_follow_spec() -> None:
    result = absolute_sub_scores(
        {
            "roe": 0.10,
            "gross_margin": 0.30,
            "net_margin": 0.20,
            "cash_ratio": 0.25,
            "roa": 0.10,
        }
    )
    assert result.values == {
        "roe": 50.0,
        "gross_margin": 80.0,
        "net_margin": 80.0,
        "cash_ratio": 80.0,
        "roa": 80.0,
    }
    assert result.missing == ()


def test_missing_input_is_zeroed_and_reported_not_silently() -> None:
    """``None`` / ``NaN`` / **根本未提供** 都算缺失 → 子分 0，但必须能在 ``missing`` 里看到。"""
    result = absolute_sub_scores({"roe": None, "roa": float("nan"), "gross_margin": 0.30})
    assert result.values["roe"] == 0.0
    assert result.values["roa"] == 0.0
    assert result.values["gross_margin"] == 80.0
    assert set(result.missing) == {"roe", "roa", "net_margin", "cash_ratio"}


def test_percentile_indicators_are_not_handled_here() -> None:
    """方式二（行业分位）归 M2-4：本模块既不产这两个键，也不静默替它们算 0。"""
    result = absolute_sub_scores(dict.fromkeys(indicators.INDICATOR_KEYS, 0.5))
    assert set(config.PERCENTILE_INDICATORS) & set(result.values) == set()
    assert set(result.values) | set(config.PERCENTILE_INDICATORS) | {"eps"} == set(
        indicators.INDICATOR_KEYS
    )


def test_bare_dict_without_unavailable_cannot_tell_zero_from_no_data() -> None:
    """**刻意的陷阱演示**：传裸字典时，``0.0`` 只能被当成"真的 0 分"。

    所以 M2-1 消费方应传它的结果对象（``RawIndicators``），缺失信息才会随行带过来；
    若确实只能传字典，就必须用 ``unavailable=`` 显式说明。
    """
    payload = dict.fromkeys(("roe", "gross_margin", "net_margin", "cash_ratio", "roa"), 0.0)

    assert absolute_sub_scores(payload).missing == ()  # 无从得知 → 当成"真的 0 分"
    flagged = absolute_sub_scores(payload, unavailable=["gross_margin"])
    assert flagged.missing == ("gross_margin",)
    assert flagged.values["gross_margin"] == 0.0


def test_deterministic() -> None:
    values = {"roe": 0.17, "roa": 0.08, "gross_margin": 0.26, "net_margin": 0.12, "cash_ratio": 0.2}
    assert absolute_sub_scores(values) == absolute_sub_scores(values)


# ── 与 M2-1 联调：真实 fixtures 端到端 ───────────────────────────────────
def test_end_to_end_with_real_fixture() -> None:
    """M2-1 指标 → M2-2 子分：手工重算 ROE 交叉验证，再确认子分确由 §4.2 插值得到。"""
    payload = json.loads(config.FIXTURES_FILE.read_text(encoding="utf-8"))
    stock = next(item for item in payload["stocks"] if item["code"] == "SH600900")

    raw = indicators.raw_indicators(stock, list(stock["financials"].values()))
    subs = absolute_sub_scores(raw)

    annual = [row for row in stock["financials"].values() if row["report_type"] == "annual"]
    # 指标**不取整**（D-18）：手算值应与实现逐位吻合，不是"取整后相等"
    manual_roe = sum(row["net_profit"] / row["equity"] for row in annual) / len(annual)

    assert subs.missing == ()
    assert raw.values["roe"] == pytest.approx(manual_roe, rel=1e-12)
    assert subs.values["roe"] == pytest.approx(
        interp_anchor(raw.values["roe"], config.ABS_ANCHORS["roe"])
    )
    assert 0.0 < subs.values["roe"] < 100.0  # 非退化：既不是全截断也不是异常值
    for value in subs.values.values():
        assert config.SUB_SCORE_MIN <= value <= config.SUB_SCORE_MAX


def test_financial_stock_gross_margin_subscore_is_zero_and_traceable() -> None:
    """工行 gross_profit 缺失 → 毛利率子分 0，且必须能追溯到"缺输入"（M2-1 的 unavailable）。"""
    payload = json.loads(config.FIXTURES_FILE.read_text(encoding="utf-8"))
    stock = next(item for item in payload["stocks"] if item["code"] == "SH601398")

    raw = indicators.raw_indicators(stock, list(stock["financials"].values()))
    subs = absolute_sub_scores(raw)  # ← 传 M2-1 的结果对象，缺失信息随行

    assert "gross_margin" in raw.unavailable
    assert subs.values["gross_margin"] == 0.0
    assert "gross_margin" in subs.missing


# ── 成长分（M2-3，《评分算法规格》§6）────────────────────────────────────
def test_cagr_compound_for_positive_series() -> None:
    """10 → 20 跨 4 个间隔 → ``(20/10)^(1/4) - 1``。"""
    assert cagr([10.0, 12.0, 14.0, 17.0, 20.0]) == pytest.approx(2**0.25 - 1)


def test_cagr_two_points_uses_single_interval() -> None:
    assert cagr([10.0, 20.0]) == pytest.approx(1.0)


@pytest.mark.parametrize("series", [[10.0], []])
def test_cagr_needs_at_least_two_points(series: list[float]) -> None:
    assert cagr(series) == 0.0


def test_cagr_degenerates_when_first_value_is_negative() -> None:
    """首值非正 → ``(y1-y0)/|y0|/n``（开方无意义、方向也会失真）。"""
    assert cagr([-10.0, 0.0, 5.0, 10.0, 20.0]) == pytest.approx(0.75)  # 30/10/4


def test_cagr_zero_first_value_returns_zero() -> None:
    assert cagr([0.0, 10.0, 20.0]) == 0.0


def test_cagr_positive_to_negative_is_negative() -> None:
    assert cagr([10.0, 0.0, -5.0]) == pytest.approx(-0.75)  # (-5-10)/10/2


def test_stability_k_constant_series_is_max() -> None:
    assert stability_k([10.0, 10.0, 10.0]) == config.K_MAX


def test_stability_k_zero_mean_is_min() -> None:
    """均值恰为 0 → 无法定义相对波动 → K_MIN。"""
    assert stability_k([1.0, -1.0]) == config.K_MIN


def test_stability_k_high_cv_is_min() -> None:
    assert stability_k([1.0, 10.0]) == pytest.approx(config.K_MIN)


def test_stability_k_mid_range_is_linear_and_rounded_to_four() -> None:
    """中间段线性；且 k **保留 4 位**（06 §6 已补明，见 D-20）。"""
    cv = 5.0 / 15.0  # [10,20]：总体 std=5，均值 15
    ratio = (cv - config.CV_LOW) / (config.CV_HIGH - config.CV_LOW)
    expected = round(config.K_MAX - (config.K_MAX - config.K_MIN) * ratio, 4)
    assert stability_k([10.0, 20.0]) == expected == 0.8778


def test_stability_k_boundaries_map_to_max_and_min() -> None:
    """cv 恰为 CV_LOW / CV_HIGH 时应分别是 K_MAX / K_MIN（构造两点序列精确命中）。"""
    assert stability_k([1.0, 1.3529411764705883]) == pytest.approx(config.K_MAX)  # cv = 0.15
    assert stability_k([1.0, 7.0]) == pytest.approx(config.K_MIN)  # cv = 6/8 = 0.75


def test_stability_k_uses_population_std_not_sample_std() -> None:
    """**口径钉子**：ddof=0（总体）与 ddof=1（样本）会给出不同的 k。

    §6 只写 ``std(s)``，已澄清为总体口径（与原型 `np.std` / JS 侧一致）——
    这条用例保证以后没人"顺手"改成样本标准差。
    """
    population = stability_k([10.0, 20.0])
    sample_cv = (2**0.5 * 5) / 15  # 样本标准差 = 7.071…，均值 15
    ratio = (sample_cv - config.CV_LOW) / (config.CV_HIGH - config.CV_LOW)
    sample = round(config.K_MAX - (config.K_MAX - config.K_MIN) * ratio, 4)

    assert population == 0.8778
    assert sample == 0.7857
    assert population != sample


def test_growth_item_caps_direction_at_plus_minus_hundred() -> None:
    """k = 1（cv ≤ CV_LOW）时可直接观察方向分的截断。"""
    assert growth_item([10.0, 11.0]) == 50.0  # cagr 10% → 方向分 50，k = 1
    assert growth_item([10.0, 12.1]) == 100.0  # cagr 21% → 截断到 +100
    assert growth_item([10.0, 5.0]) == -87.78  # cagr -50% → 截断到 -100，再乘 k


def test_growth_item_zero_growth_is_zero() -> None:
    assert growth_item([10.0, 10.0, 10.0]) == 0.0


def test_growth_item_single_point_is_zero() -> None:
    assert growth_item([10.0]) == 0.0


def test_growth_item_is_rounded_to_two_decimals() -> None:
    value = growth_item([1.0, 2.0])  # 方向 +100 × k 0.8778
    assert value == 87.78
    assert round(value, config.GROWTH_ITEM_DECIMALS) == value


def test_growth_score_total_is_sum_of_items() -> None:
    """两项增速不同 → 各自独立成项，``G`` 为两者之和（10% 增速 → 50，5% → 25）。"""
    result = growth_score([10.0, 11.0], [20.0, 21.0])
    assert result.items == {"growth_np": 50.0, "growth_ocf": 25.0}
    assert result.total == 75.0
    assert result.unavailable == ()


def test_growth_score_is_negative_for_shrinking_company() -> None:
    """§8：净利润 / 现金流为负 → **成长分取负**（这里指趋势下行）。"""
    result = growth_score([10.0, 5.0], [20.0, 10.0])
    assert result.items["growth_np"] < 0
    assert result.items["growth_ocf"] < 0
    assert result.total < 0


def test_growth_score_drops_missing_years_before_cagr() -> None:
    """有洞的序列先剔除再算：间隔数 n 必须反映**有效**点数。"""
    result = growth_score([10.0, None, 20.0], [1.0, 1.0, 1.0])
    assert result.items["growth_np"] == growth_item([10.0, 20.0]) == 87.78
    assert result.unavailable == ()


def test_growth_score_insufficient_points_is_zero_and_reported() -> None:
    result = growth_score([10.0], [None])
    assert result.items == {"growth_np": 0.0, "growth_ocf": 0.0}
    assert result.total == 0.0
    assert result.unavailable == ("growth_np", "growth_ocf")


@pytest.mark.parametrize(
    ("np_series", "ocf_series"),
    [([1.0, 10.0], [1.0, 10.0]), ([10.0, 1.0], [10.0, 1.0]), ([1.0, 10.0], [10.0, 1.0])],
)
def test_growth_total_stays_within_configured_bounds(
    np_series: list[float], ocf_series: list[float]
) -> None:
    """两项各在 ±GROWTH_MAX 内 ⇒ ``|G| ≤ GROWTH_MAX_TOTAL``（由构造保证，不额外 clamp）。"""
    total = growth_score(np_series, ocf_series).total
    assert config.GROWTH_MIN_TOTAL <= total <= config.GROWTH_MAX_TOTAL


def test_growth_total_rounds_items_before_summing() -> None:
    """**先各自取整再相加**，不是"加总后再取整"—— 取整顺序在此处分歧最难查。"""
    result = growth_score([1.0, 1.234], [1.0, 1.111])
    assert result.items["growth_np"] == round(growth_item([1.0, 1.234]), 2)
    assert result.total == round(result.items["growth_np"] + result.items["growth_ocf"], 2)


# ── 成长分：从财报行取序列（与 M2-1 共用窗口口径）───────────────────────
def _annual_rows(count: int = 5, start: int = 2020, **over) -> list[dict]:
    rows = []
    for index in range(count):
        year = start + index
        row = {
            "report_period": f"{year}-12-31",
            "report_type": "annual",
            "net_profit": 10.0 + index,
            "op_cashflow": 20.0 + index,
        }
        row.update(over)
        rows.append(row)
    return rows


def test_growth_from_rows_ignores_interim_reports() -> None:
    rows = _annual_rows() + [
        {
            "report_period": "2025-06-30",
            "report_type": "h1",
            "net_profit": 999.0,
            "op_cashflow": 999.0,
        }
    ]
    result = growth_from_rows(rows)
    assert result.items == growth_score([10.0, 11, 12, 13, 14], [20.0, 21, 22, 23, 24]).items


def test_growth_from_rows_uses_latest_five_annual_years() -> None:
    """7 年年报只取最近 5 年；数值手工可验（净利 12→16 共 4 期 ≈ 7.46% → 37.28 分）。"""
    rows = _annual_rows(count=7, start=2018)  # 2018…2024，net_profit 10…16
    result = growth_from_rows(rows)

    assert result.items["growth_np"] == 37.28
    latest_five = rows[2:]  # 2020…2024
    expected = growth_score(
        [row["net_profit"] for row in latest_five], [row["op_cashflow"] for row in latest_five]
    )
    assert result.items == expected.items


def test_growth_from_rows_respects_as_of() -> None:
    """家法 R9：``as_of`` 只取已生效期次（2024 年报要到 2025-04-30 才生效）。"""
    rows = _annual_rows(count=5)
    for index, row in enumerate(rows):
        row["effective_date"] = f"{2021 + index}-04-30"
    result = growth_from_rows(rows, as_of="2024-12-31")

    assert result.items == growth_score([10.0, 11, 12, 13], [20.0, 21, 22, 23]).items
    assert result.items != growth_from_rows(rows).items  # 不过滤的话结果不同 → 确实过滤了


def test_growth_from_rows_deterministic() -> None:
    rows = _annual_rows()
    assert growth_from_rows(rows) == growth_from_rows(rows)


def test_growth_from_rows_real_fixture_matches_manual_series() -> None:
    """真实 fixtures：从行取序列的结果 == 手工取同名序列直接算的结果。"""
    payload = json.loads(config.FIXTURES_FILE.read_text(encoding="utf-8"))
    stock = next(item for item in payload["stocks"] if item["code"] == "SH600900")
    rows = list(stock["financials"].values())

    annual = sorted(
        (row for row in rows if row["report_type"] == "annual"),
        key=lambda row: row["report_period"],
    )[-config.YEARS :]
    expected = growth_score(
        [row["net_profit"] for row in annual], [row["op_cashflow"] for row in annual]
    )

    assert growth_from_rows(rows) == expected
    assert config.GROWTH_MIN_TOTAL <= expected.total <= config.GROWTH_MAX_TOTAL
