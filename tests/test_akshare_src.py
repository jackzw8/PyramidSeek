"""AkShare 数据源实现的离线测试（任务 M1-2）。

**全部离线**：akshare 通过构造参数注入假模块（``SimpleNamespace``），
sleep 注入为记录器（不真等待），缓存目录用 ``tmp_path``。
覆盖计划 M1-2 的三条要点：重试/退避/超时、本地缓存、可识别异常。
"""

from __future__ import annotations

import time
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from src import config
from src.datasource import base
from src.datasource.akshare_src import (
    REPORT_BALANCE,
    REPORT_CASHFLOW,
    REPORT_INCOME,
    AkShareSource,
    effective_date_of,
    exchange_of,
    is_missing,
    map_financial_reports,
    market_of,
    normalize_code,
    report_period_of,
    report_type_of,
    status_of,
    to_internal_billion,
    to_records,
)

FIXED_DAY = date(2026, 10, 14)


def make_source(ak: object, tmp_path, **kwargs) -> AkShareSource:
    """构造注入了假 akshare 与固定日期、静默 sleep 的数据源。"""
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("today", lambda: FIXED_DAY)
    return AkShareSource(ak=ak, cache_dir=tmp_path, **kwargs)


# ── 单位换算与空值（家法 R10 / R11）───────────────────────────────────────
def test_to_internal_billion_converts_and_rounds() -> None:
    assert to_internal_billion(100_000_000) == 1.0  # 1 亿元
    assert to_internal_billion(1e12) == 10_000.0


def test_zero_is_a_value_not_missing() -> None:
    """0 是有效值（家法 R11）：必须返回 0.0，不能被当成空值。"""
    assert to_internal_billion(0) == 0.0
    assert is_missing(0) is False


def test_missing_values_become_none_not_zero() -> None:
    assert to_internal_billion(None) is None
    assert to_internal_billion(float("nan")) is None
    assert to_internal_billion(pd.NaT) is None
    assert is_missing(None) is True
    assert is_missing(float("nan")) is True
    assert is_missing(pd.NaT) is True


def test_to_records_normalizes_nan_to_none() -> None:
    frame = pd.DataFrame([{"报告日": "2024-12-31", "营业总收入": 1e8, "货币资金": float("nan")}])
    rows = to_records(frame)
    assert rows[0]["货币资金"] is None
    assert rows[0]["营业总收入"] == 1e8


# ── 代码 / 交易所 / 状态归一 ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "market", "expected"),
    [
        ("600900", "A", "SH600900"),
        ("90001", "A", "SH090001"),  # 不足 6 位补零
        ("000858", "A", "SZ000858"),
        ("300750", "A", "SZ300750"),
        ("830799", "A", "BJ830799"),
        ("SH600900", "A", "SH600900"),  # 已带前缀
        ("700", "HK", "HK00700"),
        ("00700", "HK", "HK00700"),
    ],
)
def test_normalize_code(raw: str, market: str, expected: str) -> None:
    assert normalize_code(raw, market) == expected


def test_normalize_code_rejects_garbage() -> None:
    with pytest.raises(base.DataSourceError):
        normalize_code("ABC", "A")


def test_market_exchange_and_status() -> None:
    assert market_of("SH600900") == "A"
    assert market_of("HK00700") == "HK"
    assert exchange_of("SH600900") == "SSE"
    assert exchange_of("SZ000858") == "SZSE"
    assert exchange_of("BJ830799") == "BSE"
    assert exchange_of("HK00700") == "HKEX"
    assert status_of("长江电力") == "normal"
    assert status_of("ST 康美") == "ST"
    assert status_of("*ST新亿") == "ST"
    assert status_of("退市海润") == "delisted"


# ── 重试 / 退避 / 超时（数据源规格 §7）───────────────────────────────────
def test_retry_then_success_sleeps_per_backoff(tmp_path) -> None:
    delays: list[float] = []
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"boom {calls['n']}")
        return [{"code": "600900", "name": "长江电力"}]

    source = make_source(
        SimpleNamespace(stock_info_a_code_name=flaky),
        tmp_path,
        sleep=delays.append,
    )
    rows = source.list_stocks("A")

    assert calls["n"] == 3
    assert delays == [1, 3]  # config.FETCH_RETRY_BACKOFF 前两项：第 3 次不必再等
    assert rows[0]["code"] == "SH600900"


def test_retry_exhausted_raises_identifiable_error(tmp_path) -> None:
    delays: list[float] = []
    calls = {"n": 0}

    def always_fail(**kwargs):
        calls["n"] += 1
        raise ConnectionError("network down")

    source = make_source(
        SimpleNamespace(stock_info_a_code_name=always_fail),
        tmp_path,
        sleep=delays.append,
    )
    with pytest.raises(base.DataSourceError) as excinfo:
        source.list_stocks("A")

    assert calls["n"] == config.FETCH_RETRY_TIMES == 3
    assert delays == [1, 3]
    err = excinfo.value
    assert err.source == "akshare"
    assert "network down" in str(err)


def test_timeout_is_enforced_and_retried(tmp_path) -> None:
    def slow(**kwargs):
        time.sleep(0.5)
        return []

    source = make_source(
        SimpleNamespace(stock_info_a_code_name=slow),
        tmp_path,
        timeout=0.05,
        retry_times=1,
    )
    with pytest.raises(base.DataSourceError, match="超时|Timeout"):
        source.list_stocks("A")


def test_health_check_returns_bool(tmp_path) -> None:
    ok = make_source(SimpleNamespace(stock_info_a_code_name=lambda **kw: []), tmp_path)
    assert ok.health_check() is True

    def boom(**kwargs):
        raise RuntimeError("down")

    bad = make_source(SimpleNamespace(stock_info_a_code_name=boom), tmp_path)
    assert bad.health_check() is False


# ── 本地缓存（原始返回按日期落盘）────────────────────────────────────────
def test_second_run_hits_cache_without_calling_source(tmp_path) -> None:
    calls = {"n": 0}

    def counter(**kwargs):
        calls["n"] += 1
        return [{"code": "600900", "name": "长江电力"}]

    ak = SimpleNamespace(stock_info_a_code_name=counter)
    first = make_source(ak, tmp_path).list_stocks("A")
    second = make_source(ak, tmp_path).list_stocks("A")

    assert calls["n"] == 1  # 第二次命中缓存，未再调用数据源
    assert first == second


def test_cache_file_lands_under_date_directory(tmp_path) -> None:
    ak = SimpleNamespace(
        stock_info_a_code_name=lambda **kw: [{"code": "600900", "name": "长江电力"}]
    )
    make_source(ak, tmp_path).list_stocks("A")
    assert (tmp_path / "2026-10-14" / "list_A.json").exists()


def test_corrupted_cache_falls_back_to_source(tmp_path) -> None:
    cache_file = tmp_path / "2026-10-14" / "list_A.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("{not json", encoding="utf-8")
    calls = {"n": 0}

    def counter(**kwargs):
        calls["n"] += 1
        return [{"code": "600900", "name": "长江电力"}]

    rows = make_source(SimpleNamespace(stock_info_a_code_name=counter), tmp_path).list_stocks("A")
    assert calls["n"] == 1
    assert rows[0]["code"] == "SH600900"


# ── 返回结构符合 M1-1 契约 ────────────────────────────────────────────────
def test_list_stocks_satisfies_contract(tmp_path) -> None:
    ak = SimpleNamespace(
        stock_info_a_code_name=lambda **kw: [
            {"code": "600900", "name": "长江电力"},
            {"code": "000858", "name": "五粮液"},
        ]
    )
    rows = make_source(ak, tmp_path).list_stocks("A")
    for row in rows:
        assert set(base.STOCK_REQUIRED_FIELDS) <= set(row)
        assert row["market"] == "A"
        assert row["industry_std"] == "申万2021"
        # 行业映射表未接入前归入「未分类」（数据源规格 §6）
        assert row["l1_name"] == config.INDUSTRY_UNCLASSIFIED
    assert rows[1]["exchange"] == "SZSE"


def test_price_daily_fills_only_requested_adjust(tmp_path) -> None:
    ak = SimpleNamespace(
        stock_zh_a_hist=lambda **kw: [
            {
                "日期": "2024-12-31",
                "开盘": 10.0,
                "最高": 11.0,
                "最低": 9.0,
                "收盘": 10.5,
                "成交量": 100,
            }
        ]
    )
    source = make_source(ak, tmp_path)

    qfq = source.price_daily("SH600900", "qfq")[0]
    assert qfq["close_qfq"] == 10.5
    assert qfq["close"] is None  # 不把前复权价当成原始价
    assert qfq["trade_date"] == "2024-12-31"

    raw = source.price_daily("SH600900", "raw")[0]
    assert raw["close"] == 10.5
    assert raw["close_qfq"] is None


def test_price_daily_rejects_unknown_adjust(tmp_path) -> None:
    source = make_source(SimpleNamespace(), tmp_path)
    with pytest.raises(ValueError, match="复权"):
        source.price_daily("SH600900", "nope")


def test_industry_tree_marks_hk_downgrade(tmp_path) -> None:
    """ADR-011：港股不直接提供 GICS，先用东财口径并在 industry_std 标注。"""
    ak = SimpleNamespace(
        stock_board_industry_name_em=lambda **kw: [
            {"板块代码": "BK0475", "板块名称": "银行", "公司家数": 42}
        ],
        stock_hk_industry_name_em=lambda **kw: [
            {"板块代码": "HKB01", "板块名称": "资讯科技", "公司家数": 12}
        ],
    )
    source = make_source(ak, tmp_path)

    a_tree = source.industry_tree("A")
    assert a_tree[0]["standard"] == "申万2021"
    assert a_tree[0]["node_name"] == "银行"
    assert a_tree[0]["level"] == 1

    hk_tree = source.industry_tree("HK")
    assert hk_tree[0]["standard"] == config.INDUSTRY_STANDARD_HK_FALLBACK


def test_unknown_market_rejected(tmp_path) -> None:
    source = make_source(SimpleNamespace(), tmp_path)
    with pytest.raises(ValueError, match="市场"):
        source.list_stocks("US")


def test_financials_hk_not_supported_yet(tmp_path) -> None:
    source = make_source(SimpleNamespace(), tmp_path)
    with pytest.raises(base.DataSourceError, match="港股"):
        source.financials("HK00700")


# ── 三表合并 → 标准层 ────────────────────────────────────────────────────
def test_report_period_and_type_helpers() -> None:
    assert report_period_of({"报告日": "2024-12-31"}) == "2024-12-31"
    assert report_period_of({"报告日": "20241231"}) == "2024-12-31"
    assert report_period_of({"报告期": "2024"}) == "2024-12-31"
    assert report_period_of({"科目": "营业收入"}) is None
    assert report_type_of("2024-12-31") == "annual"
    assert report_type_of("2025-06-30") == "h1"
    assert report_type_of("2025-03-31") == "q1"
    assert report_type_of("2025-09-30") == "q3"


def test_effective_date_is_report_period_plus_four_months() -> None:
    assert config.EFFECTIVE_LAG_MONTHS == 4
    assert effective_date_of("2024-12-31") == "2025-04-30"  # 4 月只有 30 天
    assert effective_date_of("2024-06-30") == "2024-10-30"
    assert effective_date_of("2024-09-30") == "2025-01-30"  # 跨年


def test_map_financial_reports_builds_standard_rows() -> None:
    tables = {
        REPORT_INCOME: [
            {
                "报告日": "2024-12-31",
                "营业总收入": 100e8,
                "营业总成本": 60e8,
                "归属于母公司所有者的净利润": 30e8,
            }
        ],
        REPORT_BALANCE: [
            {
                "报告日": "2024-12-31",
                "资产总计": 500e8,
                "负债合计": 200e8,
                "归属于母公司所有者权益合计": 300e8,
                "货币资金": float("nan"),
            }
        ],
        REPORT_CASHFLOW: [{"报告日": "2024-12-31", "经营活动产生的现金流量净额": 40e8}],
    }
    rows = map_financial_reports("SH600900", tables, fetched_at="2026-10-14T00:00:00Z")

    assert len(rows) == 1
    row = rows[0]
    assert set(base.FINANCIAL_REQUIRED_FIELDS) <= set(row)
    assert row["report_type"] == "annual"
    assert row["is_cumulative"] is False
    assert row["revenue"] == 100.0
    assert row["gross_profit"] == 40.0  # revenue − cost 反算
    assert row["net_profit"] == 30.0
    assert row["cash"] is None  # NaN → None，不是 0
    assert row["effective_date"] == "2025-04-30"
    assert row["source"] == "akshare"


def test_map_financial_reports_marks_interim_cumulative() -> None:
    tables = {
        REPORT_INCOME: [{"报告日": "2025-06-30", "营业总收入": 50e8, "营业总成本": 30e8}],
    }
    row = map_financial_reports("SH600900", tables, fetched_at="x")[0]
    assert row["report_type"] == "h1"
    assert row["is_cumulative"] is True  # 累计口径不可比，不进主评分（家法 R7）


def test_financials_merges_three_tables(tmp_path) -> None:
    def report(stock: str, symbol: str, **kwargs):
        if symbol == REPORT_INCOME:
            return [{"报告日": "2024-12-31", "营业总收入": 100e8, "营业总成本": 60e8}]
        if symbol == REPORT_BALANCE:
            return [{"报告日": "2024-12-31", "资产总计": 500e8, "负债合计": 200e8, "货币资金": 1e8}]
        return [{"报告日": "2024-12-31", "经营活动产生的现金流量净额": 40e8}]

    ak = SimpleNamespace(stock_financial_report_sina=report)
    rows = make_source(ak, tmp_path).financials("SH600900")

    assert len(rows) == 1
    assert rows[0]["code"] == "SH600900"
    assert rows[0]["gross_profit"] == 40.0
    assert rows[0]["cash"] == 1.0


def test_financials_uses_cache_on_second_run(tmp_path) -> None:
    calls = {"n": 0}

    def report(**kwargs):
        calls["n"] += 1
        return [{"报告日": "2024-12-31", "营业总收入": 1e8}]

    ak = SimpleNamespace(stock_financial_report_sina=report)
    make_source(ak, tmp_path).financials("SH600900")
    make_source(ak, tmp_path).financials("SH600900")
    assert calls["n"] == 3  # 三张表各取一次；第二次全部命中缓存


# ── 慢接口单独超时（D-10：实测全量列表接口约 13 分钟）────────────────────
def test_slow_endpoint_uses_slow_timeout(tmp_path) -> None:
    """全量列表接口必须走 slow_timeout，否则默认 15s 必败。"""

    def stock_info_a_code_name(**kwargs):
        time.sleep(0.15)
        return [{"code": "600900", "name": "长江电力"}]

    source = make_source(
        SimpleNamespace(stock_info_a_code_name=stock_info_a_code_name),
        tmp_path,
        timeout=0.02,
        slow_timeout=5,
    )
    assert source.list_stocks("A")[0]["code"] == "SH600900"


def test_normal_endpoint_still_uses_default_timeout(tmp_path) -> None:
    """普通接口不受慢接口宽限影响，仍按默认超时中止。"""

    def stock_zh_a_hist(**kwargs):
        time.sleep(0.15)
        return []

    source = make_source(
        SimpleNamespace(stock_zh_a_hist=stock_zh_a_hist),
        tmp_path,
        timeout=0.02,
        retry_times=1,
    )
    with pytest.raises(base.DataSourceError):
        source.price_daily("SH600900", "qfq")
