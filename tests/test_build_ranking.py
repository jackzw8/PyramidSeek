"""排行榜产物（M3-1）的测试。

钉住《产物契约》§3.3 的**主榜/单列规则**（`status=normal` + 已分类 + `eligible` 才进
`rows`，其余进 `excluded` 且 reason 取首个命中）与 §3.5 的数值口径（比率 4 位、
缺失 `null`、排序确定性）。
"""

from __future__ import annotations

import json
from pathlib import Path

from src import config
from src.build import BuildInputs
from src.build.build_ranking import EXCLUDED_COLUMNS, RANKING_COLUMNS, build_ranking
from src.engine.indicators import raw_indicators
from src.engine.industry import build_pools
from src.engine.scoring import score_universe


# ── 造数 ─────────────────────────────────────────────────────────────────
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


def _inputs(stocks: list[dict], financials: dict[str, list[dict]]) -> BuildInputs:
    scored = score_universe(stocks, financials)
    raws = {s["code"]: raw_indicators(s, financials.get(s["code"])) for s in stocks}
    return BuildInputs(
        snapshot_version="2026-10-15",
        generated_at="2026-10-15T00:00:00Z",
        stocks=tuple(stocks),
        financials=financials,
        scored=tuple(scored),
        raws=raws,
        pools=build_pools(stocks),
    )


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _simple_stocks() -> tuple[list[dict], dict[str, list[dict]]]:
    """两只同池正常股 + 四种单列情形（ST / 次新 / 未分类 / 关键指标缺失）。"""
    stocks = [
        _stock("SH600900"),  # 高分：同池
        _stock("SH600011"),  # 同池（score 更低）
        _stock("SZ000620", l1="房地产", l2="房地产开发", status="ST"),
        _stock("HK09618", l1="互联网", l2="电商", status="new"),
        _stock("SH600000", l1="未分类", l2="未分类"),
        _stock("SH600001", l1="独门行业", l2="独门子业"),  # 池内只有自己且无数据
    ]
    financials = {
        "SH600900": _rows(),
        "SH600011": _rows(net_profit=2.0),  # 更低的 ROE/规模 → 更低的分
        "SZ000620": _rows(net_profit=-3.0),
        "HK09618": _rows(years=3),
        "SH600000": _rows(),
        "SH600001": [],  # 空财报 → 加权指标全缺、池内无可填均值 → eligible=False
    }
    return stocks, financials


# ── 主榜 / 单列划分 ───────────────────────────────────────────────────────
def test_main_rows_only_normal_classified_eligible(tmp_path: Path) -> None:
    stocks, financials = _simple_stocks()
    payload = _load(build_ranking(_inputs(stocks, financials), "A", tmp_path))

    assert [row["code"] for row in payload["rows"]] == ["SH600900", "SH600011"]


def test_excluded_reasons_by_priority(tmp_path: Path) -> None:
    stocks, financials = _simple_stocks()
    inputs = _inputs(stocks, financials)

    reasons = {
        row["code"]: row["reason"]
        for row in _load(build_ranking(inputs, "A", tmp_path))["excluded"]
    }
    assert reasons == {
        "SZ000620": "ST",
        "SH600000": "行业未分类",
        "SH600001": "关键指标缺失",
    }
    hk_payload = _load(build_ranking(inputs, "HK", tmp_path / "hk"))
    assert {row["code"]: row["reason"] for row in hk_payload["excluded"]} == {"HK09618": "次新股"}

    # 单列行不带主榜字段；分数照算照列
    a_payload = _load(build_ranking(inputs, "A", tmp_path / "a2"))
    for row in a_payload["excluded"]:
        assert "rank" not in row and "isTop10pct" not in row and "level" not in row
    assert all(isinstance(row["score"], float) for row in a_payload["excluded"])


def test_market_split_keeps_only_own_market(tmp_path: Path) -> None:
    stocks, financials = _simple_stocks()
    inputs = _inputs(stocks, financials)
    a_payload = _load(build_ranking(inputs, "A", tmp_path))
    hk_payload = _load(build_ranking(inputs, "HK", tmp_path))

    assert {row["market"] for row in a_payload["rows"] + a_payload["excluded"]} == {"A"}
    assert {row["market"] for row in hk_payload["rows"] + hk_payload["excluded"]} == {"HK"}
    assert [row["code"] for row in hk_payload["excluded"]] == ["HK09618"]


# ── 排序与结构 ───────────────────────────────────────────────────────────
def test_rows_sorted_by_score_desc_and_rank_continuous(tmp_path: Path) -> None:
    stocks, financials = _simple_stocks()
    payload = _load(build_ranking(_inputs(stocks, financials), "A", tmp_path))

    scores = [row["score"] for row in payload["rows"]]
    assert scores == sorted(scores, reverse=True)
    assert [row["rank"] for row in payload["rows"]] == list(range(1, len(scores) + 1))


def test_same_score_breaks_tie_by_code(tmp_path: Path) -> None:
    stocks = [_stock("SH600002"), _stock("SH600001")]
    financials = {"SH600001": _rows(), "SH600002": _rows()}  # 完全相同 → 同分
    payload = _load(build_ranking(_inputs(stocks, financials), "A", tmp_path))

    assert [row["code"] for row in payload["rows"]] == ["SH600001", "SH600002"]


def test_columns_match_row_keys_and_order(tmp_path: Path) -> None:
    stocks, financials = _simple_stocks()
    payload = _load(build_ranking(_inputs(stocks, financials), "A", tmp_path))

    assert tuple(payload["columns"]) == RANKING_COLUMNS
    for row in payload["rows"]:
        assert tuple(row) == RANKING_COLUMNS
    for row in payload["excluded"]:
        assert tuple(row) == EXCLUDED_COLUMNS


# ── 数值口径（§3.5）──────────────────────────────────────────────────────
def test_ratio_columns_are_five_year_means_rounded_to_4(tmp_path: Path) -> None:
    stocks = [_stock("SH600900")]
    # roe = 10/200 = 0.05；gross_margin = 40/100 = 0.4；cash_ratio = 20/500 = 0.04
    payload = _load(build_ranking(_inputs(stocks, {"SH600900": _rows()}), "A", tmp_path))
    row = payload["rows"][0]

    assert row["roe"] == 0.05
    assert row["gross_margin"] == 0.4
    assert row["cash_ratio"] == 0.04
    assert row["debt_ratio"] == 0.6  # (500 − 200) ÷ 500


def test_missing_indicator_is_null_not_zero(tmp_path: Path) -> None:
    stocks = [_stock("SH601398", l1="银行", l2="银行", financial=True)]
    # 金融股：毛利全缺 → gross_margin 列应为 null（家法 R11，不用 0 顶替）
    financials = {"SH601398": _rows(gross_profit=None)}
    payload = _load(build_ranking(_inputs(stocks, financials), "A", tmp_path))

    row = payload["rows"][0]
    assert row["gross_margin"] is None
    assert row["roe"] == 0.05


def test_top10pct_is_within_group(tmp_path: Path) -> None:
    stocks, financials = _simple_stocks()
    payload = _load(build_ranking(_inputs(stocks, financials), "A", tmp_path))

    by_code = {row["code"]: row for row in payload["rows"]}
    # 池内 2 只 → ceil(2 × 10%) = 1 → 只有第一名高亮
    assert by_code["SH600900"]["isTop10pct"] is True
    assert by_code["SH600011"]["isTop10pct"] is False


def test_meta_carries_versions(tmp_path: Path) -> None:
    stocks, financials = _simple_stocks()
    payload = _load(build_ranking(_inputs(stocks, financials), "A", tmp_path))

    assert payload["meta"]["snapshotVersion"] == "2026-10-15"
    assert payload["meta"]["contractVersion"] == config.CONTRACT_VERSION == "1.2"
    assert payload["market"] == "A"


def test_write_is_deterministic(tmp_path: Path) -> None:
    stocks, financials = _simple_stocks()
    inputs = _inputs(stocks, financials)
    first = build_ranking(inputs, "A", tmp_path / "a").read_bytes()
    second = build_ranking(inputs, "A", tmp_path / "b").read_bytes()

    assert first == second


# ── 与真实 fixtures 的集成（覆盖 ST / suspended / new 三类单列）────────────
def test_real_fixtures_classification(tmp_path: Path) -> None:
    fixture = json.loads(config.FIXTURES_FILE.read_text(encoding="utf-8"))
    stocks = fixture["stocks"]
    financials = {
        stock["code"]: [{**row, "code": stock["code"]} for row in stock["financials"].values()]
        for stock in stocks
    }
    payload = _load(build_ranking(_inputs(stocks, financials), "A", tmp_path))

    codes_main = {row["code"] for row in payload["rows"]}
    codes_excluded = {row["code"]: row for row in payload["excluded"]}
    # fixtures 里 A 股 8 只：ST 1（新华联）/ suspended 1 / 其余正常
    assert "SZ000620" in codes_excluded and codes_excluded["SZ000620"]["reason"] == "ST"
    assert all(code not in codes_main for code in codes_excluded)
    assert len(codes_main) + len(codes_excluded) == 8
