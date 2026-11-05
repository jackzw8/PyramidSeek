"""`engine/indicators.py` 的测试（任务 M2-1）。

验收要求「单测覆盖边界（负值 / 零）」—— 本文件把**负值、零值、缺失**三类边界逐个钉住，
另加一条与真实 fixtures 的交叉验证，以及"指标集合与评分规格对齐"的结构性断言。
"""

from __future__ import annotations

import json

import pytest

from src import config
from src.engine import indicators
from src.engine.indicators import INDICATOR_KEYS, raw_indicators

# 默认为"整洁"的一年：各指标都是好算的圆整值
DEFAULT_FIELDS: dict[str, float] = {
    "revenue": 100.0,
    "gross_profit": 40.0,
    "net_profit": 10.0,
    "total_assets": 200.0,
    "total_liabilities": 100.0,
    "equity": 100.0,
    "cash": 20.0,
    "op_cashflow": 15.0,
    "icf": -5.0,
    "fcf": -3.0,
    "eps": 1.0,
}
DEFAULT_SHARES = 10.0


def make_rows(
    specs: list[dict] | None = None,
    *,
    count: int = 5,
    start: int = 2020,
) -> list[dict]:
    """造 ``count`` 年年报；``specs[i]`` 覆盖第 i 年的字段（每年都可单独改）。"""
    specs = specs or [{} for _ in range(count)]
    rows = []
    for index, spec in enumerate(specs):
        year = start + index
        row = {
            "code": "SH600900",
            "report_period": f"{year}-12-31",
            "report_type": "annual",
            "is_cumulative": False,
            "effective_date": f"{year + 1}-04-30",
            "source": "test",
            "fetched_at": "2026-10-14T00:00:00Z",
            **DEFAULT_FIELDS,
        }
        row.update(spec)
        rows.append(row)
    return rows


def make_stock(**over) -> dict:
    stock = {
        "code": "SH600900",
        "shares": DEFAULT_SHARES,
        "is_financial": False,
        "status": "normal",
    }
    stock.update(over)
    return stock


# ── 正常路径 ──────────────────────────────────────────────────────────────
def test_eight_indicators_are_all_defined() -> None:
    result = raw_indicators(make_stock(), make_rows())
    assert tuple(result.values) == INDICATOR_KEYS
    assert len(INDICATOR_KEYS) == 8
    assert result.unavailable == ()


def test_formulas_match_spec_section_3() -> None:
    """§3 逐项公式：逐年比值 → 5 年算术平均（这里 5 年同值，故均值 = 单年值）。"""
    result = raw_indicators(make_stock(), make_rows())
    assert result.values["roe"] == 0.1  # net_profit 10 / equity 100
    assert result.values["roa"] == 0.05  # 10 / 200
    assert result.values["gross_margin"] == 0.4  # gross_profit 40 / revenue 100
    assert result.values["net_margin"] == 0.1  # 10 / 100
    assert result.values["cash_ratio"] == 0.1  # cash 20 / 200
    assert result.values["asset_turnover"] == 0.5  # revenue 100 / 200
    assert result.values["net_profit_scale"] == 10.0  # 均值(net_profit)
    assert result.values["eps"] == 1.0  # 10 亿元 / 10 亿股 = 1 元/股


def test_average_is_per_year_ratio_not_sum_over_sum() -> None:
    """逐年比值再平均 ≠ 合计相除（§3 原文与参考实现一致，均按前者）。"""
    rows = [
        {"revenue": 100.0, "net_profit": 50.0, "total_assets": 100.0},
        {"revenue": 100.0, "net_profit": 10.0, "total_assets": 1900.0},
    ]
    result = raw_indicators(make_stock(), make_rows(rows, count=2))
    # 逐年：0.5 与 0.005263… → 均值 ≈ 0.252632（"合计相除"只有 0.03，差一个量级）
    assert result.values["roa"] == pytest.approx((50 / 100 + 10 / 1900) / 2)
    assert result.values["roa"] != pytest.approx((50 + 10) / (100 + 1900))


def test_periods_are_reported_in_ascending_order() -> None:
    result = raw_indicators(make_stock(), make_rows())
    assert result.periods == (
        "2020-12-31",
        "2021-12-31",
        "2022-12-31",
        "2023-12-31",
        "2024-12-31",
    )


def test_deterministic_same_input_same_output() -> None:
    """§9 可复现性：同输入 ⇒ 同输出。"""
    stock, rows = make_stock(), make_rows()
    assert raw_indicators(stock, rows) == raw_indicators(stock, rows)


# ── 边界：负值 ────────────────────────────────────────────────────────────
def test_negative_net_profit_produces_negative_ratios_and_scale() -> None:
    rows = make_rows([{"net_profit": -10.0} for _ in range(5)])
    result = raw_indicators(make_stock(), rows)

    assert result.values["roe"] == -0.1
    assert result.values["roa"] == -0.05
    assert result.values["net_margin"] == -0.1
    assert result.values["net_profit_scale"] == -10.0
    assert result.values["eps"] == 1.0  # D-67：直接取源字段，不随净利润变号
    assert result.values["gross_margin"] == 0.4  # 毛利口径不受净利润影响
    assert result.unavailable == ()  # 负值是有效值，不算"算不出来"


def test_negative_equity_gives_negative_roe_not_zero() -> None:
    """净资产为负（资不抵债）：公式照算，除零规则只针对分母为 0。"""
    rows = make_rows([{"equity": -100.0} for _ in range(5)])
    assert raw_indicators(make_stock(), rows).values["roe"] == -0.1


def test_mixed_sign_years_average_arithmetically() -> None:
    rows = make_rows([{"net_profit": 30.0}, {"net_profit": -10.0}, {"net_profit": 0.0}], count=3)
    result = raw_indicators(make_stock(), rows)
    assert result.values["net_profit_scale"] == pytest.approx(20 / 3)  # 指标不取整（D-18）
    assert result.values["roe"] == pytest.approx((0.3 - 0.1 + 0.0) / 3)


# ── 边界：零值（0 是有效值，家法 R11）────────────────────────────────────
def test_zero_denominator_returns_zero_not_error() -> None:
    """§3「除零返回 0」：分母为 0 的那一年贡献 0，且**参与**均值。"""
    rows = make_rows([{"revenue": 0.0}, {"revenue": 100.0}], count=2)
    result = raw_indicators(make_stock(), rows)

    assert result.values["gross_margin"] == round((0.0 + 0.4) / 2, config.RATIO_DECIMALS)
    assert result.values["net_margin"] == round((0.0 + 0.1) / 2, config.RATIO_DECIMALS)
    assert result.unavailable == ()  # 除零是"算出来是 0"，不是"算不出来"


def test_all_zero_denominators_still_return_zero() -> None:
    rows = make_rows([{"total_assets": 0.0} for _ in range(5)])
    result = raw_indicators(make_stock(), rows)
    assert result.values["roa"] == 0.0
    assert result.values["cash_ratio"] == 0.0
    assert result.values["asset_turnover"] == 0.0
    assert result.unavailable == ()


def test_zero_profit_scale_is_exact_zero() -> None:
    rows = make_rows([{"net_profit": 0.0} for _ in range(5)])
    result = raw_indicators(make_stock(), rows)
    assert result.values["net_profit_scale"] == 0.0
    assert result.values["net_margin"] == 0.0
    assert result.unavailable == ()


# ── 边界：缺失（None）────────────────────────────────────────────────────
def test_missing_field_year_is_skipped_and_averaged_over_available_years() -> None:
    rows = make_rows([{"net_profit": None}, {"net_profit": 10.0}], count=2)
    result = raw_indicators(make_stock(), rows)
    assert result.values["net_profit_scale"] == 10.0  # 只用有值的那一年
    assert result.values["roe"] == 0.1
    assert result.unavailable == ()


def test_indicator_with_no_available_year_is_zero_and_reported() -> None:
    """金融股典型：``gross_profit`` 全缺 → 毛利率取 0，但必须出现在 ``unavailable`` 里。"""
    rows = make_rows([{"gross_profit": None} for _ in range(5)])
    result = raw_indicators(make_stock(is_financial=True), rows)

    assert result.values["gross_margin"] == 0.0
    assert result.unavailable == ("gross_margin",)
    assert result.values["roe"] == 0.1  # 其他项不受影响


def test_missing_eps_is_unavailable() -> None:
    """D-67：源没给 EPS 就留空，不再用净利润÷股本补。"""
    result = raw_indicators(make_stock(), make_rows([{"eps": None}] * 5))
    assert result.values["eps"] == 0.0
    assert result.unavailable == ("eps",)


def test_zero_eps_is_valid_value() -> None:
    """EPS=0 是有效值（亏损/持平），不算缺失。"""
    result = raw_indicators(make_stock(), make_rows([{"eps": 0.0}] * 5))
    assert result.values["eps"] == 0.0
    assert result.unavailable == ()


def test_no_financials_at_all_yields_all_zero_and_all_unavailable() -> None:
    result = raw_indicators(make_stock(), [])
    assert result.values == dict.fromkeys(INDICATOR_KEYS, 0.0)
    assert set(result.unavailable) == set(INDICATOR_KEYS)
    assert result.periods == ()


def test_nan_is_treated_as_missing() -> None:
    rows = make_rows([{"net_profit": float("nan")} for _ in range(5)])
    result = raw_indicators(make_stock(), rows)
    assert result.values["net_profit_scale"] == 0.0
    assert "net_profit_scale" in result.unavailable


# ── 窗口：只吃年报、只取近 5 年 ───────────────────────────────────────────
def test_interim_reports_are_excluded() -> None:
    rows = make_rows()
    rows.append(
        {
            "code": "SH600900",
            "report_period": "2025-06-30",
            "report_type": "h1",
            "is_cumulative": True,
            "net_profit": 999.0,
            "equity": 1.0,
            "revenue": 100.0,
            "gross_profit": 40.0,
            "total_assets": 200.0,
            "cash": 20.0,
        }
    )
    result = raw_indicators(make_stock(), rows)
    assert "2025-06-30" not in result.periods
    assert result.values["net_profit_scale"] == 10.0  # 未被 999 污染


def test_only_latest_five_annual_years_are_used() -> None:
    result = raw_indicators(make_stock(), make_rows(count=7, start=2018))
    assert result.periods[0] == "2020-12-31"
    assert result.periods[-1] == "2024-12-31"
    assert len(result.periods) == config.YEARS


def test_years_parameter_overrides_window() -> None:
    result = raw_indicators(make_stock(), make_rows(count=5), years=3)
    assert result.periods == ("2022-12-31", "2023-12-31", "2024-12-31")


def test_as_of_excludes_periods_not_yet_effective() -> None:
    """家法 R9：报告期 + 4 个月才生效；给了 as_of 就只用已生效期次。"""
    rows = make_rows(count=5)  # effective_date 依次为 2021-04-30 … 2025-04-30
    result = raw_indicators(make_stock(), rows, as_of="2024-12-31")
    assert result.periods[-1] == "2023-12-31"  # 2024 年报 2025-04-30 才生效
    assert "2024-12-31" not in result.periods


def test_annual_detection_falls_back_to_report_period() -> None:
    """``report_type`` 缺失时按报告期是否为 12-31 判定（防御式，不因此丢掉整年）。"""
    rows = [{"report_period": "2024-12-31", **DEFAULT_FIELDS}]
    assert len(indicators.annual_rows(rows)) == 1
    assert indicators.annual_rows([{"report_period": "2024-06-30", **DEFAULT_FIELDS}]) == []


# ── 与评分规格的集合对齐（结构性断言，防漏算/多算指标）──────────────────
def test_indicator_keys_cover_all_configured_groups() -> None:
    """锚点表 + 分位表必须恰好等于 8 项指标（config 是唯一来源，家法 R12）。"""
    # eps 自 2026-10-14 起归入分位表（D-22），故这里不再额外并入
    assert set(config.ABS_ANCHORS) | set(config.PERCENTILE_INDICATORS) == set(INDICATOR_KEYS)
    # 比率类与规模类互斥，且并集（补上 eps）恰好是 8 项 —— 防漏算 / 多算
    assert set(config.ABS_ANCHORS) <= set(indicators.RATIO_INDICATORS)
    assert set(indicators.RATIO_INDICATORS) & set(indicators.SCALE_INDICATORS) == set()
    assert set(indicators.RATIO_INDICATORS) | set(indicators.SCALE_INDICATORS) | {"eps"} == set(
        INDICATOR_KEYS
    )


# ── 真实 fixtures 交叉验证 ───────────────────────────────────────────────
def _fixture_stock(code: str) -> tuple[dict, list[dict]]:
    payload = json.loads(config.FIXTURES_FILE.read_text(encoding="utf-8"))
    stock = next(item for item in payload["stocks"] if item["code"] == code)
    rows = list(stock["financials"].values())
    return stock, rows


def test_real_fixture_without_eps_marks_unavailable() -> None:
    """D-67：fixtures 没有 EPS 字段 → 记 unavailable，不用净利润÷股本补。"""
    stock, rows = _fixture_stock("SH600900")
    result = raw_indicators(stock, rows)

    assert "eps" in result.unavailable
    assert result.values["eps"] == 0.0


def test_real_fixture_financial_stock_reports_unavailable_gross_margin() -> None:
    """工行（``is_financial`` 且 ``gross_profit`` 为 null）→ 毛利率必须如实上报算不出来。"""
    stock, rows = _fixture_stock("SH601398")
    result = raw_indicators(stock, rows)

    assert "gross_margin" in result.unavailable
    assert result.values["gross_margin"] == 0.0
    assert result.values["roe"] != 0.0  # 其余指标照算


def test_real_fixture_loss_year_enters_average_without_clipping() -> None:
    """华能国际有亏损年：亏损必须**原样进入均值**，不能被截成 0。

    注意：5 年均值未必为负（这里实测 +6.2 亿）—— 所以断言的是"亏损年份的数值参与了口径"，
    而不是"均值为负"。**规模分的正负处理属 §4.2，不在 M2-1**（§8「负值 → 规模分取 0」）。
    """
    stock, rows = _fixture_stock("SH600011")
    annual = [row for row in rows if row["report_type"] == "annual"]
    assert any(row["net_profit"] < 0 for row in annual), "fixtures 应含亏损年"
    expected = round(sum(row["net_profit"] for row in annual) / len(annual), config.AMOUNT_DECIMALS)
    assert raw_indicators(stock, rows).values["net_profit_scale"] == expected
