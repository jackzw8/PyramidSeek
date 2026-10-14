"""清洗标准化的离线测试（任务 M1-4）。

覆盖《数据字典》§7 / 《数据源接入规格》§9 的校验规则：必填非空、勾稽、环比突变、
外键、时效、覆盖度；以及窗口裁剪（近 5 个年报 + 最新一条中报）与行情口径合并。
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from src import config
from src.pipeline import clean

TODAY = date(2026, 10, 14)


def stock_row(**overrides) -> dict:
    row = {
        "code": "SH600900",
        "name": "长江电力",
        "market": "A",
        "exchange": "SSE",
        "l1_name": "公用事业",
        "l2_name": "电力",
        "industry_std": "申万2021",
        "status": "normal",
        "listed_at": "2003-11-18",
    }
    row.update(overrides)
    return row


def financial_row(period: str = "2024-12-31", **overrides) -> dict:
    row = {
        "code": "SH600900",
        "report_period": period,
        "report_type": "annual",
        "revenue": 845.0,
        "net_profit": 325.0,
        "total_assets": 6200.0,
        "total_liabilities": 3520.0,
        "equity": 2680.0,
        "op_cashflow": 596.0,
        "fetched_at": "2026-10-14T00:00:00Z",
    }
    row.update(overrides)
    return row


# ── 股票主数据 ────────────────────────────────────────────────────────────
def test_stock_required_fields_are_enforced() -> None:
    cleaned, issues = clean.normalize_stock(stock_row(name=None), today=TODAY)
    assert cleaned["name"] is None
    assert [issue.field for issue in issues] == ["name"]
    assert issues[0].level == clean.LEVEL_ERROR
    assert issues[0].code == "SH600900"


def test_missing_industry_falls_back_to_unclassified() -> None:
    cleaned, _ = clean.normalize_stock(stock_row(l1_name=None, l2_name=None), today=TODAY)
    assert cleaned["l1_name"] == config.INDUSTRY_UNCLASSIFIED
    assert cleaned["l2_name"] == config.INDUSTRY_UNCLASSIFIED


def test_new_stock_is_tagged_new_on_both_missing_and_normal() -> None:
    """上市不足 5 年 → 打上 new（次新股单列，不参与主榜）。"""
    cleaned, issues = clean.normalize_stock(stock_row(listed_at="2023-06-01"), today=TODAY)
    assert cleaned["status"] == "new"
    assert any("次新股" in issue.message for issue in issues)

    kept, _ = clean.normalize_stock(stock_row(listed_at="2003-11-18"), today=TODAY)
    assert kept["status"] == "normal"


def test_existing_status_wins_over_new_tag() -> None:
    """ST 是既成事实，不因上市年限被改写成 new。"""
    cleaned, _ = clean.normalize_stock(stock_row(status="ST", listed_at="2023-06-01"), today=TODAY)
    assert cleaned["status"] == "ST"


def test_illegal_status_is_rejected() -> None:
    _, issues = clean.normalize_stock(stock_row(status="zombie"), today=TODAY)
    assert any(issue.field == "status" and issue.level == clean.LEVEL_ERROR for issue in issues)


def test_malformed_code_is_rejected() -> None:
    _, issues = clean.normalize_stock(stock_row(code="600900"), today=TODAY)
    assert any(issue.field == "code" and issue.level == clean.LEVEL_ERROR for issue in issues)


# ── 勾稽与环比 ────────────────────────────────────────────────────────────
def test_balance_check_passes_within_tolerance() -> None:
    assert clean.check_balance(financial_row()) is None


def test_balance_check_flags_gap_beyond_tolerance() -> None:
    issue = clean.check_balance(financial_row(total_liabilities=3000.0, equity=2000.0))
    assert issue is not None
    assert issue.level == clean.LEVEL_WARN
    assert "勾稽异常" in issue.message


def test_balance_check_skipped_when_optional_field_missing() -> None:
    """``total_liabilities`` 是可选字段：缺了就不做勾稽，而不是报警。"""
    row = financial_row()
    row.pop("total_liabilities")
    assert clean.check_balance(row) is None


def test_change_detection_flags_mom_spike() -> None:
    rows = [
        {"code": "SH600900", "report_period": "2023-12-31", "net_profit": 100.0},
        {"code": "SH600900", "report_period": "2024-12-31", "net_profit": 400.0},
    ]
    issues = clean.detect_changes(rows, field_name="net_profit")
    assert len(issues) == 1
    assert "环比突变" in issues[0].message
    assert issues[0].level == clean.LEVEL_WARN


def test_change_detection_ignores_zero_base() -> None:
    """基期为 0 时无法算变化率，跳过（0 是有效值，家法 R11）。"""
    rows = [
        {"code": "SH600900", "report_period": "2023-12-31", "net_profit": 0.0},
        {"code": "SH600900", "report_period": "2024-12-31", "net_profit": 400.0},
    ]
    assert clean.detect_changes(rows, field_name="net_profit") == []


# ── 财报序列 ──────────────────────────────────────────────────────────────
def test_financial_required_fields_route_to_pending() -> None:
    rows = [financial_row(net_profit=None)]
    cleaned, issues = clean.normalize_financial_rows("SH600900", rows, today=TODAY)
    assert cleaned == []
    assert issues[0].level == clean.LEVEL_ERROR
    assert "net_profit" in issues[0].field


def test_effective_date_is_filled_and_unborn_period_flagged() -> None:
    rows = [financial_row("2024-12-31"), financial_row("2025-12-31")]
    cleaned, issues = clean.normalize_financial_rows("SH600900", rows, today=TODAY)

    assert [item["effective_date"] for item in cleaned] == ["2025-04-30", "2026-04-30"]
    # 2025 年报要到 2026-04-30 才生效 → 此刻（2026-10-14）已生效，无告警
    assert not any(issue.field == "effective_date" for issue in issues)

    early, early_issues = clean.normalize_financial_rows(
        "SH600900", [financial_row("2025-12-31")], today=date(2026, 1, 1)
    )
    assert early[0]["effective_date"] == "2026-04-30"
    assert any("尚未生效" in issue.message for issue in early_issues)


def test_interim_reports_are_cumulative_and_only_latest_kept() -> None:
    rows = [
        financial_row("2024-12-31"),
        financial_row("2025-06-30", report_type="h1"),
        financial_row("2025-09-30", report_type="q3"),
    ]
    cleaned, _ = clean.normalize_financial_rows("SH600900", rows, today=TODAY)

    periods = [item["report_period"] for item in cleaned]
    assert periods == ["2024-12-31", "2025-09-30"]  # 只留最新一条非年报
    interim = cleaned[-1]
    assert interim["is_cumulative"] is True
    assert cleaned[0]["is_cumulative"] is False


def test_annual_window_keeps_latest_five_years() -> None:
    rows = [financial_row(f"{year}-12-31") for year in range(2015, 2025)]
    cleaned, _ = clean.normalize_financial_rows("SH600900", rows, today=TODAY)

    assert [item["report_period"] for item in cleaned] == [
        "2020-12-31",
        "2021-12-31",
        "2022-12-31",
        "2023-12-31",
        "2024-12-31",
    ]


def test_duplicate_period_keeps_latest_fetch() -> None:
    rows = [
        financial_row("2024-12-31", net_profit=100.0, fetched_at="2025-01-01T00:00:00Z"),
        financial_row("2024-12-31", net_profit=325.0, fetched_at="2026-10-14T00:00:00Z"),
    ]
    cleaned, issues = clean.normalize_financial_rows("SH600900", rows, today=TODAY)

    assert len(cleaned) == 1
    assert cleaned[0]["net_profit"] == 325.0
    assert any("重复报告期" in issue.message for issue in issues)


def test_financial_series_is_sorted_by_period() -> None:
    rows = [financial_row("2023-12-31"), financial_row("2021-12-31"), financial_row("2022-12-31")]
    cleaned, _ = clean.normalize_financial_rows("SH600900", rows, today=TODAY)
    assert [item["report_period"] for item in cleaned] == [
        "2021-12-31",
        "2022-12-31",
        "2023-12-31",
    ]


# ── 行情口径合并 ──────────────────────────────────────────────────────────
def test_price_merge_computes_adj_factor() -> None:
    raw = [
        {"code": "SH600900", "trade_date": "2024-06-28", "close": 25.0},
        {"code": "SH600900", "trade_date": "2024-06-27", "close": 20.0},
    ]
    qfq = [
        {"code": "SH600900", "trade_date": "2024-06-28", "close_qfq": 24.5},
        {"code": "SH600900", "trade_date": "2024-06-27", "close_qfq": 19.6},
    ]
    merged, issues = clean.merge_price_rows("SH600900", raw, qfq)

    assert issues == []
    assert [row["trade_date"] for row in merged] == ["2024-06-27", "2024-06-28"]  # 按日升序
    assert merged[0]["close"] == 20.0
    assert merged[0]["close_qfq"] == 19.6
    assert merged[0]["adj_factor"] == round(19.6 / 20.0, config.ADJ_FACTOR_DECIMALS)


def test_price_merge_reports_missing_side() -> None:
    raw = [{"code": "SH600900", "trade_date": "2024-06-28", "close": 25.0}]
    qfq = [{"code": "SH600900", "trade_date": "2024-06-28"}]
    merged, issues = clean.merge_price_rows("SH600900", raw, qfq)

    assert merged == []
    assert issues[0].level == clean.LEVEL_ERROR
    assert issues[0].field == "close_qfq"


def test_price_merge_warns_on_zero_close() -> None:
    raw = [{"code": "SH600900", "trade_date": "2024-06-28", "close": 0.0}]
    qfq = [{"code": "SH600900", "trade_date": "2024-06-28", "close_qfq": 0.0}]
    merged, issues = clean.merge_price_rows("SH600900", raw, qfq)

    assert merged[0]["adj_factor"] is None
    assert any("无法反算复权因子" in issue.message for issue in issues)


# ── 总入口：外键、覆盖度、日志 ────────────────────────────────────────────
def test_clean_universe_orchestrates_and_logs(caplog) -> None:
    stocks = [
        stock_row(),
        stock_row(code="SZ000858", name="五粮液", exchange="SZSE"),
        stock_row(code="SH600011", name=None),  # 必填缺失 → 待补
    ]
    financials = {
        "SH600900": [financial_row()],
        "SZ000858": [financial_row("2024-12-31", code="SZ000858")],
        "HK00700": [financial_row()],  # 外键不通过 → 丢弃
    }
    with caplog.at_level(logging.ERROR, logger="src.pipeline.clean"):
        result = clean.clean_universe(stocks, financials, today=TODAY)

    assert [row["code"] for row in result.stocks] == ["SH600900", "SZ000858"]
    assert result.pending == ["SH600011"]
    assert "HK00700" in result.rejected
    assert result.coverage["A"] == 0.0  # 两只都有财报
    assert result.degraded is False
    assert any("外键校验失败" in issue.message for issue in result.errors)
    assert "外键校验失败" in caplog.text  # 异常有日志（验收要求）


def test_coverage_over_threshold_marks_degraded() -> None:
    """单市场缺失率 > 5% → degraded（产物 meta.degraded 用它）。"""
    stocks = [stock_row(code=f"SH60000{i}") for i in range(4)]
    financials = {"SH600000": [financial_row()]}
    result = clean.clean_universe(stocks, financials, today=TODAY)

    assert result.coverage["A"] == pytest.approx(0.75)
    assert result.degraded is True


def test_market_missing_rate_handles_empty_market() -> None:
    assert clean.market_missing_rate(0, 0) == 0.0
    assert clean.market_missing_rate(10, 1) == 0.1
