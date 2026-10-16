"""AkShare 数据源实现的离线测试（任务 M1-2）。

**全部离线**：akshare 通过构造参数注入假模块（``SimpleNamespace``），
sleep 注入为记录器（不真等待），缓存目录用 ``tmp_path``。
覆盖计划 M1-2 的三条要点：重试/退避/超时、本地缓存、可识别异常。
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src import config
from src.common import (
    effective_date_of,
    exchange_of,
    is_missing,
    market_of,
    normalize_code,
    report_period_of,
    report_type_of,
    status_of,
    to_internal_billion,
    to_records,
)
from src.datasource import base
from src.datasource.akshare_src import (
    ENDPOINTS,
    REPORT_BALANCE,
    REPORT_CASHFLOW,
    REPORT_INCOME,
    AkShareSource,
    _snapshot_code,
    map_ths_financials,
    parse_ths_amount,
)

FIXED_DAY = date(2026, 10, 14)


def make_source(ak: object, tmp_path, **kwargs) -> AkShareSource:
    """构造注入了假 akshare 与固定日期、静默 sleep 的数据源。

    假 akshare 若缺**申万行业接口**（``list_stocks("A")`` 会调用它回填行业），自动补空桩；
    需要验证行业回填的用例请显式传入这三个方法。
    """
    for name in ("sw_index_first_info", "sw_index_second_info", "index_component_sw"):
        if not hasattr(ak, name):
            try:
                setattr(ak, name, lambda **_: [])
            except AttributeError:  # pragma: no cover - 只读对象
                pass
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("interval_sleep", lambda _seconds: None)  # D-42 请求节流：测试静默
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


def test_to_records_converts_dates_to_iso_strings() -> None:
    """日期列（date / Timestamp）归一为 ISO 字符串。

    不归一的话，写缓存时 ``json.dump`` 会抛 ``TypeError``（D-8 实跑踩到）。
    """
    frame = pd.DataFrame(
        [
            {
                "计入日期": date(2021, 12, 13),
                "更新时间": pd.Timestamp("2024-01-02 03:04:05"),
            }
        ]
    )
    rows = to_records(frame)

    assert rows[0]["计入日期"] == "2021-12-13"
    assert rows[0]["更新时间"].startswith("2024-01-02T03:04:05")


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


# ── 请求节流（D-42）───────────────────────────────────────────────────────
def test_throttle_sleeps_before_real_request(tmp_path) -> None:
    """真发请求（缓存未命中）前必须节流，间隔落在配置区间内。"""
    intervals: list[float] = []

    source = make_source(
        SimpleNamespace(stock_info_a_code_name=lambda **_: [{"code": "600900"}]),
        tmp_path,
        interval_sleep=intervals.append,
    )
    source.list_stocks("A")

    # list_stocks("A") = 列表 + 申万回填共 3 次取数 → 3 次节流
    assert len(intervals) == 3
    low, high = config.FETCH_INTERVAL_JITTER_SECONDS
    assert all(low <= s <= high for s in intervals)


def test_throttle_skipped_when_cache_hits(tmp_path) -> None:
    """缓存命中不再节流（否则 5000 只续跑会被 sleep 拖死）。"""
    intervals: list[float] = []

    source = make_source(
        SimpleNamespace(stock_info_a_code_name=lambda **_: [{"code": "600900", "name": "长电"}]),
        tmp_path,
        interval_sleep=intervals.append,
    )
    source.list_stocks("A")  # 首次：抓 + 写缓存
    assert intervals  # 首次确实节流了
    intervals.clear()
    source.list_stocks("A")  # 二次：应全部命中缓存

    assert intervals == []  # 命中不节流


def test_throttle_and_backoff_are_independent(tmp_path) -> None:
    """节流 sleep 与重试退避 sleep 分开记录、互不串味。"""
    backoff_delays: list[float] = []
    intervals: list[float] = []
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("boom")
        return [{"code": "600900"}]

    source = make_source(
        SimpleNamespace(stock_info_a_code_name=flaky),
        tmp_path,
        sleep=backoff_delays.append,
        interval_sleep=intervals.append,
    )
    source.list_stocks("A")

    assert intervals  # 节流发生了
    assert backoff_delays == [1]  # 退避仍是原口径（未被节流污染）
    low, high = config.FETCH_INTERVAL_JITTER_SECONDS
    assert all(low <= s <= high for s in intervals)


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


def test_partition_cache_lands_in_fixed_directory(tmp_path) -> None:
    """D-34：指定 ``partition`` 时缓存落固定子目录（冷启动跨天续传）。"""
    calls = {"n": 0}

    def counter(**kwargs):
        calls["n"] += 1
        return [{"code": "600900", "name": "长江电力"}]

    ak = SimpleNamespace(stock_info_a_code_name=counter)
    src = make_source(ak, tmp_path, partition="cold")
    src.list_stocks("A")
    assert (tmp_path / "cold" / "list_A.json").exists()
    assert not (tmp_path / "2026-10-14").exists()

    # 隔天（today 变更）重跑仍命中固定分区缓存，不再打数据源
    src_next_day = make_source(
        SimpleNamespace(stock_info_a_code_name=counter),
        tmp_path,
        partition="cold",
        today=lambda: FIXED_DAY.replace(day=15),
    )
    src_next_day.list_stocks("A")
    assert calls["n"] == 1


# ── D-37：全历史数据固定分区 + mtime TTL ─────────────────────────────────
def _fake_price_ak(calls: dict | None = None) -> SimpleNamespace:
    def hist(**kwargs):
        if calls is not None:
            calls["n"] += 1
        return [{"日期": "2024-01-02", "开盘": 1.0, "收盘": 1.1, "最高": 1.2, "最低": 0.9}]

    return SimpleNamespace(stock_zh_a_hist=hist)


def _touch(path: Path, age_days: float) -> None:
    """把文件 mtime 拨到 ``age_days`` 天前（模拟旧缓存）。"""
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))


def test_history_key_lands_in_fixed_cold_dir(tmp_path) -> None:
    """D-37：财报/行情键无论什么模式都落 cold/ 固定分区（不走日期目录）。"""
    source = make_source(_fake_price_ak(), tmp_path, stale_after=timedelta(days=20))
    source.price_daily("SH600900", "qfq")

    assert (tmp_path / "cold" / "price_SH600900_qfq.json").exists()
    assert not (tmp_path / "2026-10-14").exists()


def test_history_cache_fresh_within_ttl(tmp_path) -> None:
    """TTL 内（含当天）重复跑 → 命中缓存不重抓。"""
    calls = {"n": 0}
    source = make_source(_fake_price_ak(calls), tmp_path, stale_after=timedelta(days=20))

    source.price_daily("SH600900", "qfq")
    source.price_daily("SH600900", "qfq")

    assert calls["n"] == 1


def test_history_cache_stale_triggers_refetch_and_overwrites(tmp_path) -> None:
    """mtime 超 TTL → 视为未命中重抓，覆盖写回并刷新 mtime（续期）。"""
    calls = {"n": 0}
    source = make_source(_fake_price_ak(calls), tmp_path, stale_after=timedelta(days=20))
    source.price_daily("SH600900", "qfq")
    cache_file = tmp_path / "cold" / "price_SH600900_qfq.json"
    _touch(cache_file, age_days=30)

    source.price_daily("SH600900", "qfq")

    assert calls["n"] == 2
    assert time.time() - cache_file.stat().st_mtime < 60  # 已续期


def test_cold_baseline_never_expires(tmp_path) -> None:
    """cold 基线（stale_after=None）：再旧的缓存也命中（扩池/续传语义）。"""
    calls = {"n": 0}
    source = make_source(_fake_price_ak(calls), tmp_path, stale_after=None, partition="cold")
    source.price_daily("SH600900", "qfq")
    _touch(tmp_path / "cold" / "price_SH600900_qfq.json", age_days=365)

    source.price_daily("SH600900", "qfq")

    assert calls["n"] == 1


def test_spot_always_uses_date_partition_even_in_cold(tmp_path) -> None:
    """快照是时点数据：cold 模式也不落固定分区（防旧快照污染行情截止日）。"""
    ak = SimpleNamespace(
        stock_zh_a_spot=lambda **kw: [{"代码": "sh600519", "名称": "贵州茅台", "最新价": "1500"}]
    )
    source = make_source(ak, tmp_path, partition="cold")

    source.market_snapshot("A")

    assert (tmp_path / "2026-10-14" / "spot_A.json").exists()
    assert not (tmp_path / "cold" / "spot_A.json").exists()


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
        stock_hk_industry_name_em=lambda **kw: [
            {"板块代码": "HKB01", "板块名称": "资讯科技", "公司家数": 12}
        ],
    )
    source = make_source(ak, tmp_path)

    hk_tree = source.industry_tree("HK")
    assert hk_tree[0]["standard"] == config.INDUSTRY_STANDARD_HK_FALLBACK
    assert hk_tree[0]["node_name"] == "资讯科技"


def test_unknown_market_rejected(tmp_path) -> None:
    source = make_source(SimpleNamespace(), tmp_path)
    with pytest.raises(ValueError, match="市场"):
        source.list_stocks("US")


def test_financials_hk_not_supported_yet(tmp_path) -> None:
    source = make_source(SimpleNamespace(), tmp_path)
    with pytest.raises(base.DataSourceError, match="港股"):
        source.financials("HK00700")


# ── 报告期语义（共用 helper）────────────────────────────────────────────
def test_report_period_and_type_helpers() -> None:
    assert report_period_of({"报告日": "2024-12-31"}) == "2024-12-31"
    assert report_period_of({"报告日": "20241231"}) == "2024-12-31"
    assert report_period_of({"报告期": "2024"}) == "2024-12-31"
    assert report_period_of({"报告期": 2024}) == "2024-12-31"  # THS：年份 int
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


# ── THS 三表解析（D-39：金额为带单位字符串 → 亿元；False = 缺失）──────────────
def test_ths_endpoints_map_to_three_tables() -> None:
    """ENDPOINTS 三个 THS 键与 akshare 函数名逐一对齐（键名 `financial_{slug}_ths`）。"""
    assert ENDPOINTS["financial_income_ths"] == "stock_financial_benefit_ths"
    assert ENDPOINTS["financial_balance_ths"] == "stock_financial_debt_ths"
    assert ENDPOINTS["financial_cashflow_ths"] == "stock_financial_cash_ths"


def test_parse_ths_amount_converts_units_to_billion() -> None:
    """带单位字符串 → 亿元（2 位小数）：实测四档 万亿/亿/万/元（规格 §3.2.1）。"""
    assert parse_ths_amount("4529.44亿") == 4529.44
    assert parse_ths_amount("53.48万亿") == 534800.0  # 万亿 → ×10^4（工行资产实测）
    assert parse_ths_amount("1.05万亿") == 10500.0
    assert parse_ths_amount("7262.80万") == 0.73  # 万 → ÷10^4
    assert parse_ths_amount("-2.42万亿") == -24200.0  # 负值（投资/筹资现金流常见）
    assert parse_ths_amount("1234元") == 0.0  # 元 → ÷10^8（规格列出的单位档）
    assert parse_ths_amount("1,234.56亿") == 1234.56  # 千分位
    assert parse_ths_amount("0.00亿") == 0.0  # 字符串 0 是有效值（家法 R11）


def test_parse_ths_amount_missing_and_non_amount_formats() -> None:
    """缺失（False / None / 空串形态）与非金额格式（% / 数值型）→ None，不臆造（R11）。

    数值型不在实测域内（THS 金额恒为带单位字符串，实测 2600+ 取值零例外）→ 不推断。
    """
    for value in (False, None, "", "  ", "--", "-", "false"):
        assert parse_ths_amount(value) is None, value
    assert parse_ths_amount("23.38%") is None  # 比率不是金额
    assert parse_ths_amount(123) is None
    assert parse_ths_amount(0) is None


def test_map_ths_financials_builds_standard_rows() -> None:
    """三表（行 = 年度，列 = 带单位字符串）→ 标准层行；必填字段齐全。"""
    tables = {
        REPORT_INCOME: [
            {
                "报告期": 2025,
                "其中：营业收入": "1449.33亿",
                "其中：营业成本": "1024.09亿",
                "*营业总收入": "1449.33亿",
                "*归属于母公司所有者的净利润": "83.81亿",
            }
        ],
        REPORT_BALANCE: [
            {
                "报告期": 2025,
                "*资产合计": "4529.44亿",
                "*负债合计": "2818.47亿",
                "*归属于母公司所有者权益合计": "1004.80亿",
                "总现金": "376.85亿",
                "货币资金": "374.28亿",
            }
        ],
        REPORT_CASHFLOW: [
            {
                "报告期": 2025,
                "*经营活动产生的现金流量净额": "194.85亿",
                "*投资活动产生的现金流量净额": "-169.51亿",
                "*筹资活动产生的现金流量净额": "-76.62亿",
                "*期末现金及现金等价物余额": "266.77亿",
            }
        ],
    }
    rows = map_ths_financials("SH600188", tables, fetched_at="2026-10-16T00:00:00Z")

    assert len(rows) == 1
    row = rows[0]
    assert set(base.FINANCIAL_REQUIRED_FIELDS) <= set(row)
    assert row["report_period"] == "2025-12-31"  # 年份 int → 年末日期
    assert row["report_type"] == "annual"
    assert row["is_cumulative"] is False
    assert row["revenue"] == 1449.33
    assert row["gross_profit"] == 425.24  # 营收 − 营业成本（计算，非直取）
    assert row["net_profit"] == 83.81  # 归母，直给
    assert row["total_assets"] == 4529.44
    assert row["total_liabilities"] == 2818.47
    assert row["equity"] == 1004.8
    assert row["cash"] == 376.85  # `总现金` 优先于 `货币资金`
    assert row["op_cashflow"] == 194.85
    assert row["icf"] == -169.51
    assert row["fcf"] == -76.62
    assert row["effective_date"] == "2026-04-30"
    assert row["source"] == "ths"
    assert row["currency"] == "CNY"


def test_map_ths_financials_trims_to_recent_five_years() -> None:
    """三接口一次返回 23~31 年 → 只取近 config.YEARS 个完整财年（规格 §3.2.1）。"""
    years = list(range(2016, 2026))  # 10 个年份
    tables = {
        REPORT_INCOME: [
            {"报告期": year, "其中：营业收入": f"{100 + index}亿"}
            for index, year in enumerate(years)
        ]
    }
    rows = map_ths_financials("SH600900", tables, fetched_at="x")

    assert config.YEARS == 5
    assert [row["report_period"] for row in rows] == [
        f"{year}-12-31" for year in sorted(years)[-5:]
    ]
    assert rows[0]["revenue"] == 105.0  # 裁剪后最早一年 = 2021（100+5）


def test_map_ths_financials_bank_style_direct_parent_columns_and_cash_fallback() -> None:
    """金融股样式（工行实测）：无 `货币资金`/`总现金`/`其中：营业成本` 列。

    - ``equity`` / ``net_profit`` 取**归母口径直给列**（绕开 D-31① 列名变体）；
    - ``cash`` 三级回退到现金流表 ``*期末现金及现金等价物余额``；
    - ``gross_profit`` → ``None``（无营业成本，**不是 0**，家法 R11）。
    """
    tables = {
        REPORT_INCOME: [
            {
                "报告期": 2025,
                "其中：营业收入": "8382.70亿",
                "*营业总收入": "8382.70亿",
                "*归属于母公司所有者的净利润": "3685.62亿",
            }
        ],
        REPORT_BALANCE: [
            {
                "报告期": 2025,
                "*资产合计": "53.48万亿",
                "*负债合计": "49.24万亿",
                "*归属于母公司所有者权益合计": "4.24万亿",
            }
        ],
        REPORT_CASHFLOW: [
            {
                "报告期": 2025,
                "*经营活动产生的现金流量净额": "1.89万亿",
                "*期末现金及现金等价物余额": "1.77万亿",
            }
        ],
    }
    row = map_ths_financials("SH601398", tables, fetched_at="x")[0]

    assert row["total_assets"] == 534800.0  # 53.48万亿
    assert row["equity"] == 42400.0  # 归母权益（万亿量级），直给
    assert row["net_profit"] == 3685.62
    assert row["cash"] == 17700.0  # 第三级回退：现金流表期末余额
    assert row["gross_profit"] is None


def test_map_ths_financials_cash_prefers_total_then_currency() -> None:
    """cash 回退链：`总现金` 优先；`总现金` 缺失（False）→ 回退 `货币资金`。"""
    tables = {
        REPORT_BALANCE: [
            {"报告期": 2025, "总现金": "376.85亿", "货币资金": "374.28亿"},
            {"报告期": 2024, "总现金": False, "货币资金": "314.85亿"},
        ]
    }
    rows = map_ths_financials("SH600188", tables, fetched_at="x")
    by_period = {row["report_period"]: row for row in rows}
    assert by_period["2025-12-31"]["cash"] == 376.85  # 总现金优先
    assert by_period["2024-12-31"]["cash"] == 314.85  # 总现金 False → 货币资金


def test_map_ths_financials_revenue_falls_back_to_total_revenue() -> None:
    """revenue：`其中：营业收入` 缺失（False）→ 回退 `*营业总收入`（规格 §3.2.1）。"""
    tables = {
        REPORT_INCOME: [{"报告期": 2025, "其中：营业收入": False, "*营业总收入": "1449.33亿"}]
    }
    row = map_ths_financials("SH600188", tables, fetched_at="x")[0]
    assert row["revenue"] == 1449.33


def test_map_ths_financials_uses_parent_columns_when_merged_also_present() -> None:
    """归母列与合并口径列同现时取**归母**（THS 直给；口径与《数据字典》§3.2 一致）。"""
    tables = {
        REPORT_BALANCE: [
            {
                "报告期": 2025,
                "*归属于母公司所有者权益合计": "1004.80亿",
                "所有者权益（或股东权益）合计": "1710.97亿",
            }
        ],
        REPORT_INCOME: [
            {
                "报告期": 2025,
                "*归属于母公司所有者的净利润": "83.81亿",
                "净利润": "142.26亿",
            }
        ],
    }
    row = map_ths_financials("SH600188", tables, fetched_at="x")[0]
    assert row["equity"] == 1004.8  # 不是 1710.97（合并口径含少数股东权益）
    assert row["net_profit"] == 83.81  # 不是 142.26（含少数股东损益）


def test_map_ths_financials_missing_required_stays_none() -> None:
    """必填列缺失（False）→ None（家法 R11：不打 0；由清洗层判入「待补」）。"""
    tables = {REPORT_BALANCE: [{"报告期": 2025, "*归属于母公司所有者权益合计": False}]}
    row = map_ths_financials("SH600188", tables, fetched_at="x")[0]
    assert row["equity"] is None


# ── 行业映射：申万一级/二级 + 金融类标记（2026-10-15 实装，见 D-8）──────────
def _sw_fake_ak() -> SimpleNamespace:
    return SimpleNamespace(
        stock_info_a_code_name=lambda **_: [
            {"code": "600900", "name": "长江电力"},
            {"code": "601398", "name": "工商银行"},
        ],
        sw_index_first_info=lambda **_: [
            {"行业代码": "801160.SI", "行业名称": "公用事业", "成份个数": 100},
            {"行业代码": "801780.SI", "行业名称": "银行", "成份个数": 42},
        ],
        sw_index_second_info=lambda **_: [
            {"行业代码": "801161.SI", "行业名称": "电力", "上级行业": "公用事业", "成份个数": 80},
            {
                "行业代码": "801782.SI",
                "行业名称": "国有大型银行",
                "上级行业": "银行",
                "成份个数": 6,
            },
        ],
        index_component_sw=lambda symbol, **_: (
            [{"证券代码": "600900"}] if str(symbol) == "801161" else [{"证券代码": "601398"}]
        ),
    )


def test_list_stocks_fills_sw_industry_and_financial_flag(tmp_path) -> None:
    """A 股行业按**申万**回填（l1/l2 两级）；一级为金融类 → ``is_financial=True``（§8）。"""
    source = make_source(_sw_fake_ak(), tmp_path)
    rows = {row["code"]: row for row in source.list_stocks("A")}

    assert rows["SH600900"]["l1_name"] == "公用事业"
    assert rows["SH600900"]["l2_name"] == "电力"
    assert rows["SH600900"]["l1_code"] == "801160"
    assert rows["SH600900"]["is_financial"] is False
    assert rows["SH601398"]["l1_name"] == "银行"
    assert rows["SH601398"]["l2_name"] == "国有大型银行"
    assert rows["SH601398"]["is_financial"] is True


def test_industry_tree_uses_sw_nodes_for_a(tmp_path) -> None:
    """A 股行业树 = 申万一级 + 二级；二级 ``parent_code`` 指向一级；口径 = 申万2021。"""
    source = make_source(_sw_fake_ak(), tmp_path)
    nodes = source.industry_tree("A")

    firsts = [node for node in nodes if node["level"] == 1]
    seconds = [node for node in nodes if node["level"] == 2]
    assert {node["node_name"] for node in firsts} == {"公用事业", "银行"}
    assert {node["node_name"] for node in seconds} == {"电力", "国有大型银行"}
    power = next(node for node in seconds if node["node_name"] == "电力")
    assert power["parent_code"] == "801160"
    assert all(node["standard"] == "申万2021" for node in nodes)


# ── financials()：THS 三接口调用与缓存（D-39）────────────────────────────
def _ths_fake_ak(calls: list[tuple[str, dict]] | None = None) -> SimpleNamespace:
    """三个 THS 接口的假模块（各返回一张 2025 年报；可选记录调用参数）。"""

    def record(name: str, rows: list[dict]):
        def fn(**kwargs):
            if calls is not None:
                calls.append((name, kwargs))
            return rows

        return fn

    return SimpleNamespace(
        stock_financial_benefit_ths=record(
            "income",
            [
                {
                    "报告期": 2025,
                    "其中：营业收入": "100亿",
                    "其中：营业成本": "60亿",
                    "*归属于母公司所有者的净利润": "30亿",
                }
            ],
        ),
        stock_financial_debt_ths=record(
            "balance",
            [
                {
                    "报告期": 2025,
                    "*资产合计": "500亿",
                    "*负债合计": "200亿",
                    "*归属于母公司所有者权益合计": "300亿",
                    "货币资金": "1亿",
                }
            ],
        ),
        stock_financial_cash_ths=record(
            "cashflow", [{"报告期": 2025, "*经营活动产生的现金流量净额": "40亿"}]
        ),
    )


def test_financials_calls_ths_endpoints_with_plain_code(tmp_path) -> None:
    """financials() 走 THS 三接口：symbol=**纯 6 位代码**、indicator=按年度（§3.2.1 传参）。"""
    calls: list[tuple[str, dict]] = []
    rows = make_source(_ths_fake_ak(calls), tmp_path).financials("SH600188")

    assert len(rows) == 1
    assert rows[0]["code"] == "SH600188"
    assert rows[0]["revenue"] == 100.0
    assert rows[0]["gross_profit"] == 40.0  # 100 − 60
    assert rows[0]["cash"] == 1.0
    assert sorted(name for name, _ in calls) == ["balance", "cashflow", "income"]
    for _, kwargs in calls:
        assert kwargs["symbol"] == "600188"  # 不带 SH 前缀
        assert kwargs["indicator"] == "按年度"


def test_financials_cache_lands_in_cold_with_existing_key_convention(tmp_path) -> None:
    """缓存键沿用既有约定 ``financial_{code}_{slug}``、落 ``cold/`` 固定分区（D-37）。"""
    make_source(_ths_fake_ak(), tmp_path).financials("SH600188")

    cold = tmp_path / "cold"
    assert (cold / "financial_SH600188_income.json").exists()
    assert (cold / "financial_SH600188_balance.json").exists()
    assert (cold / "financial_SH600188_cashflow.json").exists()


def test_financials_uses_cache_on_second_run(tmp_path) -> None:
    calls: list[tuple[str, dict]] = []
    make_source(_ths_fake_ak(calls), tmp_path).financials("SH600188")
    make_source(_ths_fake_ak(calls), tmp_path).financials("SH600188")
    assert len(calls) == 3  # 三张表各取一次；第二次全部命中缓存


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


def test_single_stock_endpoints_use_medium_timeout(tmp_path) -> None:
    """单股批量接口（行情 / 三表）走**中等超时**（300s）：0.15s 的延迟不再被 0.02s 误杀（D-8）。"""

    def stock_zh_a_hist(**kwargs):
        time.sleep(0.15)
        return [{"日期": "2024-01-02", "开盘": 1.0, "收盘": 1.1, "最高": 1.2, "最低": 0.9}]

    source = make_source(
        SimpleNamespace(stock_zh_a_hist=stock_zh_a_hist),
        tmp_path,
        timeout=0.02,
        slow_timeout=5,
    )

    rows = source.price_daily("SH600900", "qfq")
    assert len(rows) == 1


# ── 全市场快照（D-32）────────────────────────────────────────────────────
def test_snapshot_code_normalization() -> None:
    """新浪快照代码 → 标准层格式：A 股小写前缀 → 大写；港股 5 位数字补 HK 前缀。"""
    assert _snapshot_code("sh600519", "A") == "SH600519"
    assert _snapshot_code("sz000001", "A") == "SZ000001"
    assert _snapshot_code("bj920000", "A") == "BJ920000"
    assert _snapshot_code("00001", "HK") == "HK00001"
    assert _snapshot_code("", "A") is None
    assert _snapshot_code(None, "A") is None


def test_market_snapshot_maps_rows_and_skips_bad_codes(tmp_path) -> None:
    """快照行映射：代码归一、名称去空格、字符串数值转 float；停牌（最新价 `--`）保留 None。"""

    def stock_zh_a_spot():
        return [
            {
                "代码": "sh600519",
                "名称": "贵州 茅台",
                "最新价": "1412.0",
                "今开": "1400.0",
                "最高": "1420.0",
                "最低": "1395.0",
                "昨收": "1405.0",
                "成交量": "30000",
            },
            {"代码": "sz000858", "名称": "五 粮 液", "最新价": "--", "成交量": "--"},
            {"代码": "not-a-code", "名称": "坏行"},
        ]

    source = make_source(SimpleNamespace(stock_zh_a_spot=stock_zh_a_spot), tmp_path)
    rows = source.market_snapshot("A")

    assert [row["code"] for row in rows] == ["SH600519", "SZ000858"]
    assert rows[0]["name"] == "贵州茅台"
    assert rows[0]["latest_price"] == 1412.0
    assert rows[0]["open"] == 1400.0
    assert rows[0]["prev_close"] == 1405.0
    assert rows[1]["latest_price"] is None  # 停牌（--）→ None
    assert rows[1]["name"] == "五粮液"


def test_market_snapshot_rejects_unknown_market(tmp_path) -> None:
    source = make_source(SimpleNamespace(stock_zh_a_spot=lambda: []), tmp_path)
    try:
        source.market_snapshot("US")
        raise AssertionError("应拒绝未知市场")
    except ValueError as exc:
        assert "不支持的市场" in str(exc)


def test_price_daily_maps_raw_mode_to_empty_adjust(tmp_path) -> None:
    """内部模式 ``raw`` → akshare 的 ``adjust=""``（不映射会被当非法参数，D-8 实跑踩到）。"""
    seen: list[str] = []

    def stock_zh_a_hist(**kwargs):
        seen.append(kwargs.get("adjust"))
        return [{"日期": "2024-01-02", "开盘": 1.0, "收盘": 1.1, "最高": 1.2, "最低": 0.9}]

    source = make_source(SimpleNamespace(stock_zh_a_hist=stock_zh_a_hist), tmp_path)
    source.price_daily("SH600900", "raw")
    source.price_daily("SH600900", "qfq")

    assert seen == ["", "qfq"]


def test_default_timeout_still_applies_to_fast_endpoints(tmp_path) -> None:
    """非慢接口仍按默认超时中止（避免无差别放宽）。"""

    def ordinary_endpoint(**kwargs):  # 函数名不在 SLOW_ENDPOINTS
        time.sleep(0.15)
        return []

    source = make_source(SimpleNamespace(), tmp_path, timeout=0.02, retry_times=1)
    with pytest.raises(TimeoutError):
        source._invoke(ordinary_endpoint)
