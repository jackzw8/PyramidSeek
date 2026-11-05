"""组装总分（M2-5）的测试：《评分算法规格》§7 合成与排序 + §11 口径清单。

三个容易错、且错了会**静默**的地方，各有钉子：
- `Q = round(Σ(sub×w), 2) ÷ 100` —— 先取整再除（反例见 test_quality_score_rounds_...）；
- `score = round(clamp(Q+G, 0, 1000), 2)` —— 先 clamp 再取整；
- 雷达六维里 `asset_turnover` / `eps` 取**分位**子分，其余取绝对值子分。
"""

from __future__ import annotations

import json

import pytest

from src import config
from src.common import round_half_up
from src.engine import indicators
from src.engine.scoring import (
    RADAR_KEYS,
    absolute_sub_scores,
    growth_from_rows,
    interp_anchor,
    quality_score,
    radar_scores,
    score_universe,
    total_score,
)


# ── 造数：最小可用的标准层股票行 + 年报行 ────────────────────────────────
def _stock(
    code: str,
    *,
    l1: str = "公用事业",
    l2: str = "电力",
    financial: bool = False,
    shares: float = 100.0,
) -> dict:
    market = "HK" if code.startswith("HK") else "A"
    return {
        "code": code,
        "market": market,
        "l1_name": l1,
        "l2_name": l2,
        "industry_std": config.INDUSTRY_STANDARD[market],
        "is_financial": financial,
        "shares": shares,
        "status": "normal",
    }


def _rows(
    *,
    years: int = 5,
    revenue: float = 100.0,
    net_profit: float = 10.0,
    gross_profit: float | None = 40.0,
    cash: float = 20.0,
    equity: float | None = 200.0,
    total_assets: float = 500.0,
    op_cashflow: float = 15.0,
) -> list[dict]:
    out: list[dict] = []
    for index in range(years):
        out.append(
            {
                "report_period": f"{2020 + index}-12-31",
                "report_type": "annual",
                "revenue": revenue,
                "net_profit": net_profit,
                "gross_profit": gross_profit,
                "cash": cash,
                "equity": equity,
                "total_assets": total_assets,
                "op_cashflow": op_cashflow,
            }
        )
    return out


def _fixture_universe() -> tuple[list[dict], dict[str, list[dict]]]:
    payload = json.loads(config.FIXTURES_FILE.read_text(encoding="utf-8"))
    stocks = list(payload["stocks"])
    return stocks, {stock["code"]: list(stock["financials"].values()) for stock in stocks}


# ── Q / 总分 / 雷达 ──────────────────────────────────────────────────────
def test_quality_score_rounds_the_weighted_sum_before_dividing() -> None:
    """**取整顺序钉子**：`round(Σ(sub×w), 2) ÷ 100`，不是 `round(Σ÷100, 2)`。

    参考实现 `engine.js` 就是先 `toFixed(2)` 再除 100；两者在边界上差 0.01，
    M2-6 双引擎逐项比对必然失败（06 §11 已写死这条）。
    """
    subs = {key: 12.3456749 for key in config.WEIGHTS}
    weighted = sum(12.3456749 * weight for weight in config.WEIGHTS.values())

    expected = round_half_up(weighted, config.Q_DECIMALS) / 100
    assert quality_score(subs) == pytest.approx(expected) == pytest.approx(123.4567)
    assert quality_score(subs) != pytest.approx(123.46)  # 反证：先除后取整会得到这个值


def test_quality_score_bounds_and_missing_handling() -> None:
    assert quality_score({key: 0.0 for key in config.WEIGHTS}) == 0.0
    assert quality_score({key: 100.0 for key in config.WEIGHTS}) == pytest.approx(config.Q_MAX)
    partial = {key: 100.0 for key in list(config.WEIGHTS)[:3]}
    assert 0.0 < quality_score(partial) < config.Q_MAX  # 未提供的按 0 计，不报错


def test_quality_score_accepts_sub_scores_object() -> None:
    subs = absolute_sub_scores({key: 50.0 for key in config.ABS_ANCHORS})
    assert quality_score(subs) == pytest.approx(quality_score(subs.values))


def test_total_score_clamps_before_rounding() -> None:
    assert total_score(10.0, -50.0) == 0.0  # 下限
    assert total_score(990.0, 50.0) == config.SCORE_MAX  # 上限（茅台 fixtures 就撞这一条）
    assert total_score(123.456, 0.0) == pytest.approx(123.46)  # 半进位


def test_radar_scores_takes_two_dimensions_from_percentile() -> None:
    """§11：`asset_turnover` / `eps` 用**分位**子分，其余用绝对值子分。"""
    radar = radar_scores(
        {key: 11.0 for key in config.ABS_ANCHORS},
        {"asset_turnover": 88.0, "eps": 77.0},
    )

    assert tuple(radar) == RADAR_KEYS  # 顶点顺序 = 契约 §3.4（顶 ROE 顺时针）
    assert radar["asset_turnover"] == 88.0
    assert radar["eps"] == 77.0
    assert radar["roe"] == 11.0


# ── 组装：字段形状 ────────────────────────────────────────────────────────
def test_subs_contain_exactly_the_weighted_indicators() -> None:
    """**`subs` 只能是加权的那 6 项**：`eps` / `net_margin` 只进雷达（DDL 无 `sub_eps` 等列）。"""
    stocks = [_stock("SH600900"), _stock("SH600901")]
    financials = {code: _rows() for code in ("SH600900", "SH600901")}
    row = score_universe(stocks, financials)[0]

    assert set(row.subs) == set(config.WEIGHTS)
    assert "eps" not in row.subs
    assert "eps" in row.radar


def test_snapshot_row_columns_match_score_snapshot_ddl(tmp_path) -> None:
    """组装结果 → `score_snapshot` 一行：**列名与 DDL 逐字一致**（多一列少一列都要红）。"""
    from src.pipeline import store  # 局部导入：只有测试允许跨层

    row = score_universe([_stock("SH600900")], {"SH600900": _rows()})[0].snapshot_row("2026-10-15")
    columns = store.table_columns("score_snapshot", db_path=tmp_path / "s.duckdb")

    assert set(row) == set(columns)
    assert row["snapshot_version"] == "2026-10-15"


# ── 排序与高亮（§7）───────────────────────────────────────────────────────
def test_rank_within_group_and_top10pct() -> None:
    """组内按 score 降序；`is_top10pct = rank ≤ ceil(n × 10%)`。"""
    stocks = [_stock(f"SH60090{index}") for index in range(3)]
    financials = {
        stock["code"]: _rows(net_profit=10.0 + index) for index, stock in enumerate(stocks)
    }
    rows = sorted(score_universe(stocks, financials), key=lambda row: row.code)

    # 净利随 code 递增 → 分数递增 → code 顺序上的 rank 递减（最高分是 rank 1）
    assert [row.rank_in_group for row in rows] == [3, 2, 1]
    assert [row.is_top10pct for row in rows] == [False, False, True]  # ceil(3×0.1)=1
    assert [row.code for row in sorted(rows, key=lambda r: r.rank_in_group)] == [
        "SH600902",
        "SH600901",
        "SH600900",
    ]


def test_top10pct_cut_uses_ceil() -> None:
    """20 只 → 前 **2** 名（ceil(20×0.1)=2）。"""
    stocks = [_stock(f"SH600{index:03d}") for index in range(20)]
    financials = {
        stock["code"]: _rows(net_profit=10.0 + index) for index, stock in enumerate(stocks)
    }
    rows = score_universe(stocks, financials)

    assert sum(row.is_top10pct for row in rows) == 2


def test_ranking_is_group_scoped_and_stable_for_ties() -> None:
    """组内排名互不干扰；同分按 code 升序 → 结果**确定**（参考实现按输入顺序，M2-6 以本文为准）。"""
    stocks = [
        _stock("SH600001", l1="公用事业", l2="电力"),
        _stock("SH600002", l1="食品饮料", l2="白酒"),
    ]
    financials = {"SH600001": _rows(), "SH600002": _rows()}

    forward = {row.code: row.rank_in_group for row in score_universe(stocks, financials)}
    backward = {row.code: row.rank_in_group for row in score_universe(stocks[::-1], financials)}
    assert forward == backward == {"SH600001": 1, "SH600002": 1}


# ── §8 缺失值处理 ────────────────────────────────────────────────────────
def test_missing_indicator_is_filled_with_pool_mean() -> None:
    """池内样本足够（≥ MIN_FILL_SAMPLES）时，缺失项用**同池均值**填充并记进 `filled`。"""
    stocks = [_stock(f"SH60000{i}") for i in (1, 2, 3)]
    financials = {
        "SH600001": _rows(gross_profit=40.0),
        "SH600002": _rows(gross_profit=30.0),
        "SH600003": _rows(gross_profit=None),  # 算不出来
    }
    rows = {row.code: row for row in score_universe(stocks, financials)}

    assert rows["SH600003"].filled == ("gross_margin",)
    assert rows["SH600003"].unfilled == ()
    # 填充的是**池均值** (0.40 + 0.30) / 2 = 0.35，不是某一只的值
    assert rows["SH600003"].subs["gross_margin"] == pytest.approx(
        interp_anchor(0.35, config.ABS_ANCHORS["gross_margin"])
    )


def test_missing_indicator_not_filled_when_pool_too_small() -> None:
    """池内有效样本 < MIN_FILL_SAMPLES 时**不填充**（1~2 只代表不了行业，D-65）。

    原实现只要池里有值就填 —— 实测出现过「14 只保险股里 1 只有毛利率，其余 13 只
    全用它的值」，分数被单只样本带偏。不足时记 ``unfilled``，非金融股会进单列。
    """
    stocks = [_stock("SH600001"), _stock("SH600002")]
    financials = {
        "SH600001": _rows(gross_profit=40.0),
        "SH600002": _rows(gross_profit=None),
    }
    rows = {row.code: row for row in score_universe(stocks, financials)}

    assert rows["SH600002"].filled == ()
    assert rows["SH600002"].unfilled == ("gross_margin",)


def test_financial_missing_indicator_is_exempt_from_ineligibility() -> None:
    """§8 金融类「毛利率/周转率不适用 → 单列」：单列 = 照算照列，不因此判「不入主榜」。"""
    stocks = [_stock("SH600001", l1="银行", l2="股份制银行", financial=True)]
    row = score_universe(stocks, {"SH600001": _rows(equity=None)})[0]

    assert "roe" in row.unfilled
    assert row.eligible is True


def test_non_financial_missing_weighted_indicator_is_ineligible() -> None:
    """池里也没均值可填 + 属加权指标 → 该股**不入主榜**（§8「关键字段缺失则不入榜」）。"""
    stocks = [_stock("SH600001", l1="公用事业", l2="电力")]
    row = score_universe(stocks, {"SH600001": _rows(equity=None)})[0]

    assert row.unfilled == ("roe",)
    assert row.eligible is False
    assert row.subs["roe"] == 0.0  # 保持 0，不猜


# ── 真实 fixtures 端到端 ──────────────────────────────────────────────────
def test_score_universe_on_real_fixtures_is_sane() -> None:
    """12 只 fixtures：分数有界、字段齐、排名从 1 开始。"""
    stocks, financials = _fixture_universe()
    rows = score_universe(stocks, financials)

    assert len(rows) == len(stocks)
    for row in rows:
        assert config.SCORE_MIN <= row.score <= config.SCORE_MAX
        assert config.Q_MIN <= row.quality <= config.Q_MAX
        assert config.GROWTH_MIN_TOTAL <= row.growth <= config.GROWTH_MAX_TOTAL
        assert set(row.subs) == set(config.WEIGHTS)
        assert set(row.radar) == set(RADAR_KEYS)
        assert all(0.0 <= value <= 100.0 for value in row.radar.values())
        assert row.compare_level in {"L1", "L2"}
        assert row.rank_in_group >= 1
    assert any(row.score == config.SCORE_MAX for row in rows)  # clamp 上限确实被触发过


def test_score_universe_is_deterministic() -> None:
    stocks, financials = _fixture_universe()
    assert score_universe(stocks, financials) == score_universe(stocks, financials)


def test_bank_stays_eligible_but_records_unfilled() -> None:
    """工行：`gross_profit` 全为 null → 记进 `unfilled`，但**仍 eligible**（§8 单列）。"""
    stocks, financials = _fixture_universe()
    bank = next(row for row in score_universe(stocks, financials) if row.code == "SH601398")

    assert "gross_margin" in bank.unfilled
    assert bank.eligible is True


def test_quality_and_growth_match_manual_computation() -> None:
    """交叉验证：Q 由 6 项子分按权重算、G 取 `growth_from_rows` 的结果。"""
    stocks, financials = _fixture_universe()
    rows = {row.code: row for row in score_universe(stocks, financials)}
    stock = next(item for item in stocks if item["code"] == "SH600900")

    expected_growth = growth_from_rows(financials["SH600900"]).total
    expected_quality = quality_score(rows["SH600900"].subs)

    assert rows["SH600900"].growth == expected_growth
    assert rows["SH600900"].quality == pytest.approx(expected_quality)
    assert rows["SH600900"].score == total_score(expected_quality, expected_growth)
    assert indicators.raw_indicators(stock, financials["SH600900"]).values["roe"] > 0


def test_compare_group_comes_from_industry_pool() -> None:
    """组信息来自 M2-4 的对比池（回退到一级时 compare_level = L1）。"""
    stocks, financials = _fixture_universe()
    for row in score_universe(stocks, financials):
        assert row.compare_group  # 非空
        assert row.group_key.endswith(row.compare_group)
