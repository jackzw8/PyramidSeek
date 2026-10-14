"""`engine/industry.py` 的测试（任务 M2-4）。

验收要求「回退规则单测通过」—— 本文件把 §5 的回退判据（二级样本 < 30 → 一级）、
§4.2 方式二的分位公式、以及 **D-19 裁定**（负值规模分强制 0、但池内不排除负值成员）
逐条钉住；另与 `batch.resolve_pools`（M0 的占位实现）做语义交叉验证，防止两套口径分叉。
"""

from __future__ import annotations

import json

import pytest

from src import batch, config
from src.engine import indicators
from src.engine.industry import (
    build_pools,
    percentile_score,
    pool_sub_scores,
    unclassified_codes,
)

UNCLASSIFIED = config.INDUSTRY_UNCLASSIFIED


def make_stock(
    code: str,
    *,
    market: str = "A",
    l1: str = "公用事业",
    l2: str = "电力",
) -> dict:
    return {
        "code": code,
        "market": market,
        "l1_name": l1,
        "l2_name": l2,
        "industry_std": config.INDUSTRY_STANDARD[market],
    }


def make_group(
    count: int,
    *,
    prefix: str,
    market: str = "A",
    l1: str = "公用事业",
    l2: str = "电力",
) -> list[dict]:
    return [
        make_stock(f"{market}{prefix}{index:03d}", market=market, l1=l1, l2=l2)
        for index in range(count)
    ]


# ── §4.2 方式二：分位公式 ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("values", "value", "expected"),
    [
        ([1.0, 2.0, 3.0, 4.0], 4.0, 87.5),  # 最高：(3 + 0.5) / 4 × 100
        ([1.0, 2.0, 3.0, 4.0], 1.0, 12.5),  # 最低：(0 + 0.5) / 4 × 100
        ([1.0, 2.0, 2.0, 3.0], 2.0, 50.0),  # 并列：(1 + 1) / 4 × 100
        ([1.0, 2.0, 3.0], 2.0, 50.0),  # 中位：(1 + 0.5) / 3 × 100
        ([5.0, 5.0, 5.0], 5.0, 50.0),  # 全并列 → 50
    ],
)
def test_percentile_score_matches_spec_formula(
    values: list[float], value: float, expected: float
) -> None:
    assert percentile_score(values, value) == pytest.approx(expected)


@pytest.mark.parametrize("values", [[], [3.0]])
def test_single_member_pool_returns_fifty(values: list[float]) -> None:
    """§4.2 明文：n ≤ 1 → 50（单股池无从比较）。"""
    assert percentile_score(values, 3.0) == 50.0


def test_pool_includes_the_stock_itself() -> None:
    """池内**含自己**：若把自己排除，最高值会得 (1+0.5)/2×100=75 而不是 66.67。"""
    assert percentile_score([1.0, 2.0, 3.0], 3.0) == pytest.approx(250 / 3)


def test_value_outside_pool_range_hits_bounds() -> None:
    """低于池内最小值 → 0（less 与 eq 都为 0）；高于最大值 → 100。"""
    assert percentile_score([1.0, 2.0, 3.0], 9.0) == 100.0
    assert percentile_score([1.0, 2.0, 3.0], 0.0) == 0.0


# ── §5 对比池：回退规则 ───────────────────────────────────────────────────
def test_thirty_members_stay_at_level_two() -> None:
    """**恰好 30 不回退**（判据是 `< min_size`）—— 最容易写错的 off-by-one。"""
    pools = build_pools(make_group(30, prefix="600"))
    assert {info.level for info in pools.values()} == {"L2"}
    assert all(info.use_fallback is False for info in pools.values())
    assert pools["A600000"].group == "电力"
    assert pools["A600000"].size == 30


def test_below_min_falls_back_to_level_one() -> None:
    stocks = make_group(29, prefix="600", l1="公用事业", l2="电力") + make_group(
        6, prefix="601", l1="公用事业", l2="水务"
    )
    pools = build_pools(stocks)

    electric = pools["A600000"]
    assert electric.use_fallback is True
    assert electric.level == config.COMPARE_LEVEL_FALLBACK
    assert electric.group == "公用事业"
    assert electric.size == 35  # 回退后按一级池计
    # 同属一级但二级不同的另一只股票，也回退到同一个一级池
    assert pools["A601000"].group == "公用事业"


def test_min_size_is_configurable() -> None:
    pools = build_pools(make_group(5, prefix="600"), min_size=5)
    assert pools["A600000"].use_fallback is False


def test_markets_are_never_mixed() -> None:
    """分市场独立：A 股与港股的**同名行业**各自成池（§5）。"""
    pools = build_pools(
        make_group(3, prefix="600", market="A") + make_group(4, prefix="0", market="HK")
    )
    assert pools["A600000"].size == 3
    assert pools["HK0000"].size == 4
    assert pools["HK0000"].market == "HK"


def test_members_are_sorted_and_consistent_with_size() -> None:
    info = build_pools(make_group(4, prefix="600"))["A600000"]
    assert list(info.members) == sorted(info.members)
    assert info.size == len(info.members)


def test_unclassified_stocks_get_no_pool() -> None:
    stocks = make_group(3, prefix="600") + [
        make_stock("SH601398", l1=UNCLASSIFIED, l2=UNCLASSIFIED)
    ]
    pools = build_pools(stocks)

    assert "SH601398" not in pools  # 未分类不参与分位（config 既定口径）
    assert unclassified_codes(stocks) == ("SH601398",)
    assert pools["A600000"].size == 3  # 未分类既不进池、也不改变池大小


# ── 分位子分：池聚合 + D-19 裁定 ──────────────────────────────────────────
def test_pool_sub_scores_follow_percentile_formula() -> None:
    pools = build_pools(make_group(3, prefix="600"))
    values = {"A600000": 1.0, "A600001": 2.0, "A600002": 3.0}

    subs = pool_sub_scores("asset_turnover", values, pools)
    assert subs["A600000"] == pytest.approx(50 / 3)  # (0 + 0.5) / 3 × 100
    assert subs["A600002"] == pytest.approx(250 / 3)  # (2 + 0.5) / 3 × 100


def test_negative_scale_sub_score_is_forced_to_zero_but_stays_in_pool() -> None:
    """**D-19 裁定**：负值规模分强制 0，但该成员**仍在池内**参与分位基准。

    若错误地把负值成员排除出池（池变成 [1,2,3]），A600001 的分位会变成 16.67 而不是 37.5。
    """
    pools = build_pools(make_group(4, prefix="600"))
    values = {"A600000": -5.0, "A600001": 1.0, "A600002": 2.0, "A600003": 3.0}

    subs = pool_sub_scores("net_profit_scale", values, pools)
    assert subs["A600000"] == 0.0  # §8：负值不给规模分
    assert subs["A600001"] == pytest.approx(37.5)  # (1 + 0.5) / 4 × 100
    assert subs["A600003"] == pytest.approx(87.5)


def test_negative_asset_turnover_still_uses_percentile() -> None:
    """§8 只点名"规模分"，D-19 的强制 0 **不施加**于周转率 —— 钉住范围，防止以后误扩。"""
    pools = build_pools(make_group(3, prefix="600"))
    values = {"A600000": -1.0, "A600001": 1.0, "A600002": 2.0}

    subs = pool_sub_scores("asset_turnover", values, pools)
    assert subs["A600000"] == pytest.approx(50 / 3)  # 按分位给分，未强制 0


def test_missing_value_yields_none_not_zero() -> None:
    """无值 → ``None``（不是 0 分）：§8 要求用行业均值填充，M2-5 才能分辨（家法 R11）。"""
    pools = build_pools(make_group(3, prefix="600"))
    subs = pool_sub_scores("net_profit_scale", {"A600000": None, "A600001": 5.0}, pools)

    assert subs["A600000"] is None
    assert subs["A600002"] is None  # 未提供也算无值
    assert subs["A600001"] == 50.0  # 池内只剩自己有值 → n = 1 → 50


def test_missing_values_do_not_pollute_the_pool_baseline() -> None:
    pools = build_pools(make_group(3, prefix="600"))
    values = {"A600000": 10.0, "A600001": None, "A600002": 20.0}

    subs = pool_sub_scores("asset_turnover", values, pools)
    assert subs["A600002"] == pytest.approx(75.0)  # 池内有效值只有 [10, 20]


def test_unclassified_codes_are_absent_from_results() -> None:
    stocks = make_group(3, prefix="600") + [
        make_stock("SH601398", l1=UNCLASSIFIED, l2=UNCLASSIFIED)
    ]
    pools = build_pools(stocks)
    subs = pool_sub_scores("net_profit_scale", {"SH601398": 100.0, "A600000": 1.0}, pools)

    assert "SH601398" not in subs
    assert set(subs) == {"A600000", "A600001", "A600002"}


def test_non_percentile_indicator_raises() -> None:
    with pytest.raises(KeyError, match="不走行业分位"):
        pool_sub_scores("roe", {"A600000": 0.2}, {})


def test_deterministic() -> None:
    stocks = make_group(5, prefix="600")
    values = {stock["code"]: float(index) for index, stock in enumerate(stocks)}
    assert pool_sub_scores("net_profit_scale", values, build_pools(stocks)) == pool_sub_scores(
        "net_profit_scale", values, build_pools(stocks)
    )


# ── 与 M0 的 `batch.resolve_pools` 交叉验证（防两套口径分叉）───────────────
def test_real_fixtures_pools_match_batch_resolver() -> None:
    payload = json.loads(config.FIXTURES_FILE.read_text(encoding="utf-8"))
    stocks = list(payload["stocks"])

    pools = build_pools(stocks)
    legacy = batch.resolve_pools(stocks)

    assert set(pools) == set(legacy)
    for code, info in pools.items():
        assert info.level == legacy[code]["level"]
        assert info.group == legacy[code]["group"]
        assert info.size == legacy[code]["count"]
        assert info.use_fallback is legacy[code]["use_fallback"]
    assert all(info.use_fallback for info in pools.values())  # 12 只样本 → 全部二级不足 30


def test_real_fixtures_percentile_subs_are_bounded() -> None:
    """M2-1 指标 → M2-4 分位：真实 fixtures 上的端到端（子分必在 [0,100]）。"""
    payload = json.loads(config.FIXTURES_FILE.read_text(encoding="utf-8"))
    stocks = list(payload["stocks"])
    pools = build_pools(stocks)

    values = {
        stock["code"]: indicators.raw_indicators(stock, list(stock["financials"].values())).values[
            "net_profit_scale"
        ]
        for stock in stocks
    }
    subs = pool_sub_scores("net_profit_scale", values, pools)

    assert set(subs) == set(pools)
    assert all(value is None or 0.0 <= value <= 100.0 for value in subs.values())
    # D-19：凡是**均值为负**的股票，其规模分子分必须是 0（不是分位值）
    for code, value in values.items():
        if value is not None and value < 0:
            assert subs[code] == 0.0
