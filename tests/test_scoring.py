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
from src.engine.scoring import absolute_sub_score, absolute_sub_scores, interp_anchor

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
    manual_roe = round(
        sum(row["net_profit"] / row["equity"] for row in annual) / len(annual),
        config.RATIO_DECIMALS,
    )

    assert subs.missing == ()
    assert raw.values["roe"] == manual_roe  # 指标本身手算一致
    assert subs.values["roe"] == pytest.approx(interp_anchor(manual_roe, config.ABS_ANCHORS["roe"]))
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
