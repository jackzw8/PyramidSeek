"""个股详情产物（M3-2）的测试：《产物契约》§3.4 结构 + §3.5 数值/空值口径。"""

from __future__ import annotations

import json
from pathlib import Path

from src import config
from src.build import BuildInputs
from src.build.build_stock import build_stock
from src.engine.indicators import raw_indicators
from src.engine.industry import build_pools
from src.engine.scoring import RADAR_KEYS, score_universe

# 契约 §3.4 的字段顺序（不含可选 kline）
EXPECTED_KEYS = (
    "code",
    "name",
    "market",
    "exchange",
    "l1",
    "l2",
    "group",
    "level",
    "industryStd",
    "listedAt",
    "score",
    "quality",
    "growth",
    "growthDetail",
    "qualitySubs",
    "filled",
    "unfilled",
    "radar",
    "years",
    "profitability",
    "structure",
    "operation",
    "cashflow",
    "peers",
)


def _stock(
    code: str,
    *,
    l1: str = "公用事业",
    l2: str = "电力",
    status: str = "normal",
    financial: bool = False,
    shares: float = 100.0,
) -> dict:
    market = "HK" if code.startswith("HK") else "A"
    return {
        "code": code,
        "name": f"公司{code}",
        "market": market,
        "l1_code": "801160",
        "l1_name": l1,
        "l2_code": "801161",
        "l2_name": l2,
        "industry_std": config.INDUSTRY_STANDARD[market],
        "exchange": "SSE" if market == "A" else "HKEX",
        "listed_at": "2000-01-01",
        "is_financial": financial,
        "shares": shares,
        "status": status,
    }


def _rows(
    *,
    years: int = 5,
    revenue: float | None = 100.0,
    net_profit: float | None = 10.0,
    gross_profit: float | None = 40.0,
    cash: float | None = 20.0,
    equity: float | None = 200.0,
    total_assets: float | None = 500.0,
    op_cashflow: float | None = 15.0,
    icf: float | None = -5.0,
    fcf: float | None = -3.0,
) -> list[dict]:
    return [
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
            "icf": icf,
            "fcf": fcf,
        }
        for index in range(years)
    ]


def _inputs(
    stocks: list[dict], financials: dict[str, list[dict]], prices: dict | None = None
) -> BuildInputs:
    scored = score_universe(stocks, financials)
    raws = {s["code"]: raw_indicators(s, financials.get(s["code"])) for s in stocks}
    return BuildInputs(
        snapshot_version="2026-10-15",
        generated_at="2026-10-15T00:00:00Z",
        stocks=tuple(stocks),
        financials=financials,
        prices=prices or {},
        scored=tuple(scored),
        raws=raws,
        pools=build_pools(stocks),
    )


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _payload(tmp_path: Path, code: str = "SH600900") -> dict:
    stocks = [_stock("SH600900"), _stock("SH600011")]
    financials = {"SH600900": _rows(), "SH600011": _rows(net_profit=2.0)}
    return _load(build_stock(_inputs(stocks, financials), code, tmp_path))


# ── 结构 ─────────────────────────────────────────────────────────────────
def test_payload_keys_and_order(tmp_path: Path) -> None:
    payload = _payload(tmp_path)

    assert tuple(payload) == EXPECTED_KEYS
    assert payload["code"] == "SH600900"
    assert payload["exchange"] == "SSE"
    assert payload["industryStd"] == "申万2021"
    assert payload["listedAt"] == "2000-01-01"


def test_radar_and_subs_keys_follow_declared_order(tmp_path: Path) -> None:
    payload = _payload(tmp_path)

    assert tuple(payload["radar"]) == RADAR_KEYS
    assert tuple(payload["qualitySubs"]) == tuple(config.WEIGHTS)
    assert tuple(payload["growthDetail"]) == ("netProfit", "opCashflow")
    assert isinstance(payload["growthDetail"]["netProfit"], float)


# ── 四张财务表（§3.4 / §3.5）─────────────────────────────────────────────
def test_tables_align_with_years_and_carry_null(tmp_path: Path) -> None:
    payload = _payload(tmp_path)

    assert payload["years"] == ["2020", "2021", "2022", "2023", "2024"]
    for table in ("profitability", "structure", "operation", "cashflow"):
        for series in payload[table].values():
            assert len(series) == len(payload["years"])

    assert payload["profitability"]["roe"] == [0.05] * 5
    assert payload["profitability"]["eps"] == [0.1] * 5
    assert payload["structure"]["debt_ratio"] == [0.6] * 5
    # 标准层无应收/固定资产字段 → 恒 null（已知限制）
    assert payload["operation"]["ar_turnover"] == [None] * 5
    assert payload["operation"]["fa_turnover"] == [None] * 5
    assert payload["cashflow"]["ncf"] == [7.0] * 5  # 15 − 5 − 3
    assert payload["cashflow"]["cash"] == [20.0] * 5


def test_missing_year_is_null_not_zero(tmp_path: Path) -> None:
    stocks = [_stock("SH601398", l1="银行", l2="银行", financial=True)]
    financials = {"SH601398": _rows(gross_profit=None)}  # 金融股毛利全缺
    payload = _load(build_stock(_inputs(stocks, financials), "SH601398", tmp_path))

    assert payload["profitability"]["gross_margin"] == [None] * 5
    assert payload["radar"]["gross_margin"] == 0.0  # 子分侧按 0（M2 口径），原值侧 null


def test_short_history_uses_available_years(tmp_path: Path) -> None:
    stocks = [_stock("HK09618")]
    payload = _load(build_stock(_inputs(stocks, {"HK09618": _rows(years=3)}), "HK09618", tmp_path))

    assert payload["years"] == ["2020", "2021", "2022"]
    assert payload["profitability"]["roe"] == [0.05] * 3


# ── peers ────────────────────────────────────────────────────────────────
def test_peers_include_self_sorted_by_roe(tmp_path: Path) -> None:
    payload = _payload(tmp_path)

    assert [row["code"] for row in payload["peers"]] == ["SH600900", "SH600011"]
    assert payload["peers"][0]["roe5y"] == 0.05
    assert payload["peers"][1]["roe5y"] == 0.01


def test_unclassified_stock_has_empty_peers(tmp_path: Path) -> None:
    stocks = [_stock("SH600000", l1="未分类", l2="未分类")]
    payload = _load(build_stock(_inputs(stocks, {"SH600000": _rows()}), "SH600000", tmp_path))

    assert payload["peers"] == []
    assert payload["level"] == "" and payload["group"] == ""


# ── kline（可选）─────────────────────────────────────────────────────────
def test_kline_absent_without_price_data(tmp_path: Path) -> None:
    assert "kline" not in _payload(tmp_path)


def test_kline_aggregates_monthly_qfq(tmp_path: Path) -> None:
    prices = {
        "SH600900": [
            {
                "trade_date": "2024-01-02",
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "adj_factor": 1.0,
                "close_qfq": 10.5,
            },
            {
                "trade_date": "2024-01-31",
                "open": 10.5,
                "high": 12.0,
                "low": 10.0,
                "close": 11.0,
                "adj_factor": 1.0,
                "close_qfq": 11.0,
            },
            {
                "trade_date": "2024-02-01",
                "open": 11.0,
                "high": 11.5,
                "low": 10.8,
                "close": 11.2,
                "adj_factor": 0.5,
                "close_qfq": 5.6,
            },
        ]
    }
    stocks = [_stock("SH600900")]
    inputs = _inputs(stocks, {"SH600900": _rows()}, prices)
    payload = _load(build_stock(inputs, "SH600900", tmp_path))

    kline = payload["kline"]
    assert kline["months"] == ["2024-01", "2024-02"]
    assert kline["o"] == [10.0, 5.5]  # 第二月按 adj_factor 0.5 换算
    assert kline["h"] == [12.0, 5.75]
    assert kline["l"] == [9.5, 5.4]
    assert kline["c"] == [11.0, 5.6]  # close_qfq 已是前复权价，不再乘因子


def test_kline_hfq_divides_by_first_factor(tmp_path: Path) -> None:
    # 首日 adj_factor 0.5 → hfq = qfq ÷ 0.5（期初归一到原始价，契约 §3.4）
    prices = {
        "SH600900": [
            {
                "trade_date": "2024-01-02",
                "open": 5.0,
                "high": 5.5,
                "low": 4.8,
                "close": 5.2,
                "adj_factor": 0.5,
                "close_qfq": 2.6,
            },
            {
                "trade_date": "2024-02-01",
                "open": 5.2,
                "high": 5.6,
                "low": 5.0,
                "close": 5.4,
                "adj_factor": 1.0,
                "close_qfq": 5.4,
            },
        ]
    }
    stocks = [_stock("SH600900")]
    payload = _load(
        build_stock(_inputs(stocks, {"SH600900": _rows()}, prices), "SH600900", tmp_path)
    )

    kline = payload["kline"]
    assert kline["c"] == [2.6, 5.4]  # 前复权 = close_qfq
    assert kline["hfq"]["c"] == [5.2, 10.8]  # 后复权 = qfq ÷ 0.5
    assert kline["hfq"]["o"] == [5.0, 10.4]  # 1 月原始价（5.0 × 0.5 ÷ 0.5）
    assert kline["hfq"]["h"] == [5.5, 11.2]


def test_kline_omits_hfq_when_first_factor_is_negative(tmp_path: Path) -> None:
    """首日 ``adj_factor`` ≤ 0（长期高分红 → 前复权早期为负）→ 不输出 ``hfq``。

    真实案例：长江电力 2003-11-18 前复权价 -8.33 元 → 比率无意义，照算会反号（D-8 实跑）。
    """
    prices = {
        "SH600900": [
            {
                "trade_date": "2003-11-18",
                "open": 6.0,
                "high": 6.2,
                "low": 5.9,
                "close": 6.18,
                "adj_factor": -1.348,
                "close_qfq": -8.33,
            },
            {
                "trade_date": "2024-01-02",
                "open": 20.0,
                "high": 20.5,
                "low": 19.8,
                "close": 20.2,
                "adj_factor": 1.0,
                "close_qfq": 20.2,
            },
        ]
    }
    stocks = [_stock("SH600900")]
    payload = _load(
        build_stock(_inputs(stocks, {"SH600900": _rows()}, prices), "SH600900", tmp_path)
    )

    assert "kline" in payload
    assert "hfq" not in payload["kline"]  # 可选子对象缺省 → 前端禁用复权切换


def test_kline_b_level_uses_qfq_ohlc_and_omits_hfq(tmp_path: Path) -> None:
    """B 级降级（D-32 决策 1）：只有 qfq 序列（``adj_factor`` 全缺）→ 月K 主序列
    直接用前复权 OHLC（factor 兜底 1.0），**不输出 ``hfq``**（避免把前复权序列
    原样当后复权，语义错误）。"""
    prices = {
        "SH600900": [
            {
                "trade_date": "2024-01-02",
                "open": 19.5,
                "high": 19.8,
                "low": 19.2,
                "close": None,
                "close_qfq": 19.6,
                "adj_factor": None,
                "volume": 1000,
            },
            {
                "trade_date": "2024-01-31",
                "open": 19.9,
                "high": 20.1,
                "low": 19.4,
                "close": None,
                "close_qfq": 20.0,
                "adj_factor": None,
                "volume": 1200,
            },
        ]
    }
    stocks = [_stock("SH600900")]
    inputs = _inputs(stocks, {"SH600900": _rows()}, prices)
    payload = _load(build_stock(inputs, "SH600900", tmp_path))

    kline = payload["kline"]
    assert kline["months"] == ["2024-01"]
    assert kline["o"] == [19.5]
    assert kline["h"] == [20.1]
    assert kline["l"] == [19.2]
    assert kline["c"] == [20.0]
    assert "hfq" not in kline


def test_write_is_deterministic(tmp_path: Path) -> None:
    stocks = [_stock("SH600900"), _stock("SH600011")]
    financials = {"SH600900": _rows(), "SH600011": _rows(net_profit=2.0)}
    inputs = _inputs(stocks, financials)

    assert (
        build_stock(inputs, "SH600900", tmp_path / "a").read_bytes()
        == build_stock(inputs, "SH600900", tmp_path / "b").read_bytes()
    )
