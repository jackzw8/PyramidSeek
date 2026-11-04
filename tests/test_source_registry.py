"""M5-1 财报源注册表与映射的离线单测（不联网）。

覆盖：默认源、可选集、非法选择、腾讯 A 股否决、金额解析 / 港币折算 / 东财映射。
"""

from __future__ import annotations

import pytest

from src import config
from src.datasource import registry
from src.datasource.base import DataSourceError
from src.datasource.em_src import map_em_a_financials, map_em_hk_financials
from src.datasource.tx_hk_src import map_tx_hk_financials, parse_tx_amount


def test_defaults_are_eastmoney_for_both_markets() -> None:
    assert registry.validate_selection(None) == {"A": "em", "HK": "em"}
    assert registry.validate_selection({"A": "sina"}) == {"A": "sina", "HK": "em"}


def test_available_sources_exclude_tencent_for_a_shares() -> None:
    assert registry.available_sources("A") == ["sina", "em", "ths"]
    assert registry.available_sources("HK") == ["tx", "em"]
    # D-55 实测否决：腾讯 A 股无可用的结构化财报接口
    assert "tx" not in registry.available_sources("A")


def test_invalid_selection_fails_loudly() -> None:
    with pytest.raises(ValueError, match="无可用源"):
        registry.validate_selection({"A": "tx"})
    with pytest.raises(ValueError, match="无可用源"):
        registry.build_source("HK", "ths")
    with pytest.raises(ValueError, match="未知市场"):
        registry.validate_selection({"US": "em"})


def test_tencent_source_rejects_a_share_codes() -> None:
    src = registry.build_source("HK", "tx")
    with pytest.raises(DataSourceError, match="腾讯 A 股"):
        src.financials("SH600519")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,561.54亿元", 1561.54),
        ("-3,644.44亿元", -3644.44),
        ("5000万元", 0.5),
        ("--", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_tx_amount(raw: str | None, expected: float | None) -> None:
    assert parse_tx_amount(raw) == expected


def test_tx_hk_amounts_are_converted_from_display_currency() -> None:
    """腾讯金额是港币显示口径，必须 ÷ 每期转换汇率还原原始币种（D-53）。"""
    periods = [
        {
            "period": "2025-12-31",
            "fields": {
                "营业收入": "1,110亿元",
                "营业成本": "-555亿元",  # 腾讯口径为负值
                "归属母公司所有者净利润": "111亿元",
                "资产总计": "2,220亿元",
                "归属母公司股东权益": "1,110亿元",
                "经营活动产生现金净流量": "333亿元",
                "投资活动产生的现金流量净额": "-111亿元",
                "筹资活动产生的现金流量净额": "-55.5亿元",
            },
            "currency": "CNY",
            "rate": 1.11,
        }
    ]
    rows = map_tx_hk_financials("HK00700", periods)
    assert len(rows) == 1
    row = rows[0]
    assert row["revenue"] == 1000.0
    assert row["gross_profit"] == 500.0  # 营业成本负值已取绝对值再算毛利
    assert row["net_profit"] == 100.0
    assert row["equity"] == 1000.0
    assert row["op_cashflow"] == 300.0
    assert row["currency"] == "CNY"
    assert row["source"] == "tx"
    assert row["report_type"] == "annual"
    assert row["is_cumulative"] is False


def test_em_a_wide_table_mapping() -> None:
    """东财 A 股宽表（元）→ 亿元；归母权益直给；只取近 config.YEARS 个年报期。"""
    tables: dict[str, list[dict]] = {"资产负债表": [], "利润表": [], "现金流量表": []}
    for year in range(2018, 2026):
        tables["利润表"].append(
            {
                "REPORT_DATE": f"{year}-12-31 00:00:00",
                "TOTAL_OPERATE_INCOME": 1000e8 * year / 2018,
                "OPERATE_COST": 400e8,
                "PARENT_NETPROFIT": 200e8,
            }
        )
        tables["资产负债表"].append(
            {
                "REPORT_DATE": f"{year}-12-31 00:00:00",
                "TOTAL_ASSETS": 3000e8,
                "TOTAL_LIABILITIES": 1000e8,
                "TOTAL_PARENT_EQUITY": 2000e8,
                "MONETARYFUNDS": 500e8,
            }
        )
        tables["现金流量表"].append(
            {
                "REPORT_DATE": f"{year}-12-31 00:00:00",
                "NETCASH_OPERATE": 250e8,
                "NETCASH_INVEST": -50e8,
                "NETCASH_FINANCE": -30e8,
            }
        )
    rows = map_em_a_financials("SH600519", tables)
    assert len(rows) == config.YEARS
    last = rows[-1]
    assert last["report_period"] == "2025-12-31"
    assert last["net_profit"] == 200.0
    assert last["equity"] == 2000.0
    assert last["total_assets"] == 3000.0
    assert last["op_cashflow"] == 250.0
    assert last["currency"] == "CNY"
    assert last["source"] == "em"


def test_em_hk_long_table_handles_cross_stock_subject_names() -> None:
    """东财港股科目名跨股不统一：00700「股东权益」/ 00939「归属于母公司股东权益」。"""

    def rows(equity_name: str, revenue_name: str) -> dict[str, list[dict]]:
        def item(period: str, name: str, value: float) -> dict:
            return {"REPORT_DATE": period, "STD_ITEM_NAME": name, "AMOUNT": value}

        p = "2025-12-31 00:00:00"
        return {
            "资产负债表": [
                item(p, "资产总计", 2000e8),
                item(p, equity_name, 1000e8),
                item(p, "现金及等价物", 300e8),
            ],
            "利润表": [
                item(p, revenue_name, 800e8),
                item(p, "股东应占溢利", 200e8),
            ],
            "现金流量表": [item(p, "经营业务现金净额", 250e8)],
        }

    a = map_em_hk_financials("HK00700", rows("股东权益", "营业额"))
    b = map_em_hk_financials("HK00939", rows("归属于母公司股东权益", "经营收入总额"))
    for result in (a, b):
        assert len(result) == 1
        row = result[0]
        assert row["equity"] == 1000.0
        assert row["revenue"] == 800.0
        assert row["net_profit"] == 200.0
        assert row["currency"] is None  # 实测接口无 CURRENCY 列，不猜
        assert row["source"] == "em"
