"""元数据 / 行业产物（M3-3）的测试：《产物契约》§3.1 / §3.2。"""

from __future__ import annotations

import json
from pathlib import Path

from src import config
from src.build import BuildInputs
from src.build.build_meta import build_industries, build_meta, build_search_index
from src.engine.indicators import raw_indicators
from src.engine.industry import build_pools
from src.engine.scoring import score_universe


def _stock(
    code: str,
    *,
    l1_code: str = "801160",
    l1: str = "公用事业",
    l2_code: str = "801161",
    l2: str = "电力",
    industry_std: str | None = None,
) -> dict:
    market = "HK" if code.startswith("HK") else "A"
    return {
        "code": code,
        "name": f"公司{code}",
        "market": market,
        "l1_code": l1_code,
        "l1_name": l1,
        "l2_code": l2_code,
        "l2_name": l2,
        "industry_std": industry_std or config.INDUSTRY_STANDARD[market],
        "exchange": "SSE" if market == "A" else "HKEX",
        "listed_at": "2000-01-01",
        "is_financial": False,
        "shares": 100.0,
        "status": "normal",
    }


def _rows(years: int = 5) -> list[dict]:
    return [
        {
            "report_period": f"{2020 + index}-12-31",
            "report_type": "annual",
            "revenue": 100.0,
            "net_profit": 10.0,
            "gross_profit": 40.0,
            "cash": 20.0,
            "equity": 200.0,
            "total_assets": 500.0,
            "op_cashflow": 15.0,
        }
        for index in range(years)
    ]


def _inputs(stocks: list[dict], **overrides) -> BuildInputs:
    financials = {s["code"]: _rows() for s in stocks}
    scored = score_universe(stocks, financials)
    raws = {s["code"]: raw_indicators(s, financials.get(s["code"])) for s in stocks}
    base = {
        "snapshot_version": "2026-10-15",
        "generated_at": "2026-10-15T00:00:00Z",
        "stocks": tuple(stocks),
        "financials": financials,
        "scored": tuple(scored),
        "raws": raws,
        "pools": build_pools(stocks),
    }
    base.update(overrides)
    return BuildInputs(**base)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── meta.json ────────────────────────────────────────────────────────────
def test_meta_carries_versions_dates_and_disclaimer(tmp_path: Path) -> None:
    stocks = [_stock("SH600900")]
    inputs = _inputs(stocks, data_date_financial="2024-12-31", data_date_price=None, degraded=True)
    payload = _load(build_meta(inputs, tmp_path))

    assert payload["snapshotVersion"] == "2026-10-15"
    assert payload["contractVersion"] == config.CONTRACT_VERSION
    assert payload["formulaVersion"] == config.FORMULA_VERSION
    assert payload["generatedAt"] == "2026-10-15T00:00:00Z"
    assert payload["weights"] == dict(config.WEIGHTS)  # 前端「评分构成」直接读（R12）
    assert sum(payload["weights"].values()) == 1000
    assert payload["dataDate"] == {"financial": "2024-12-31"}  # 无行情 → price 键缺省
    assert payload["degraded"] is True
    assert config.DISCLAIMER in payload["notes"]  # 家法 R20


def test_meta_notes_include_degrade_trail(tmp_path: Path) -> None:
    inputs = _inputs([_stock("SH600900")], notes=("list_stocks:A 降级到 official",))
    payload = _load(build_meta(inputs, tmp_path))

    assert "降级" in payload["notes"]
    assert payload["notes"].endswith(config.DISCLAIMER)


def test_meta_uses_actual_industry_standard(tmp_path: Path) -> None:
    hk = _stock("HK00700", industry_std=config.INDUSTRY_STANDARD_HK_FALLBACK)
    payload = _load(build_meta(_inputs([hk]), tmp_path))

    # 港股走东财降级路径时，产物必须标注**实际口径**（ADR-011），不是理想值 GICS
    assert payload["industryStandard"]["HK"] == config.INDUSTRY_STANDARD_HK_FALLBACK
    assert payload["industryStandard"]["A"] == config.INDUSTRY_STANDARD["A"]  # 无样本 → 回落


def test_meta_picks_dominant_standard(tmp_path: Path) -> None:
    stocks = [_stock("SH600900"), _stock("SH600011"), _stock("SH601398", industry_std="旧口径")]
    payload = _load(build_meta(_inputs(stocks), tmp_path))

    assert payload["industryStandard"]["A"] == "申万2021"


# ── industries.json ──────────────────────────────────────────────────────
def test_industries_tree_and_counts(tmp_path: Path) -> None:
    stocks = [
        _stock("SH600900"),
        _stock("SH600011"),
        _stock("SZ000858", l1_code="801120", l1="食品饮料", l2_code="801123", l2="白酒"),
        _stock("SH600000", l1_code="", l1="未分类", l2_code="", l2="未分类"),
    ]
    payload = _load(build_industries(_inputs(stocks), tmp_path))

    assert payload["markets"] == ["A", "HK"]
    assert payload["snapshotVersion"] == "2026-10-15"
    tree = {node["market"]: node for node in payload["tree"]}
    assert [node["code"] for node in tree["A"]["l1"]] == ["801120", "801160"]
    assert [node["code"] for node in tree["A"]["l2"]] == ["801123", "801161"]
    assert tree["A"]["l2"][0] == {"code": "801123", "name": "白酒", "parent": "801120"}
    assert tree["HK"]["l1"] == []

    # counts 由 M2 的 pools 派生：两只同池（< 30 → 回退一级）+ 食品饮料单只
    counts = payload["counts"]
    assert counts["A:公用事业"] == {
        "market": "A",
        "group": "公用事业",
        "level": "L1",
        "count": 2,
        "use_fallback": True,
    }
    assert counts["A:食品饮料"]["count"] == 1
    assert "A:未分类" not in counts  # 未分类不进池（engine.industry 既定口径）


def test_industries_is_deterministic(tmp_path: Path) -> None:
    stocks = [_stock("SH600900"), _stock("SH600011")]
    inputs = _inputs(stocks)

    assert (
        build_industries(inputs, tmp_path / "a").read_bytes()
        == build_industries(inputs, tmp_path / "b").read_bytes()
    )


# ── search-index.json（契约 §3.6）────────────────────────────────────────
def test_search_index_entries_sorted_with_pinyin(tmp_path: Path) -> None:
    stocks = [_stock("SH600519"), _stock("HK00700")]
    stocks[0]["name"] = "贵州茅台"
    stocks[1]["name"] = "腾讯控股"
    payload = _load(build_search_index(_inputs(stocks), tmp_path))

    assert [entry["code"] for entry in payload["entries"]] == ["HK00700", "SH600519"]  # code 升序
    by_code = {entry["code"]: entry for entry in payload["entries"]}
    assert by_code["SH600519"]["py"] == "gzmt"
    assert by_code["HK00700"]["py"] == "txkg"
    assert by_code["HK00700"]["market"] == "HK"
    assert payload["contractVersion"] == config.CONTRACT_VERSION


def test_search_index_keeps_non_han_characters(tmp_path: Path) -> None:
    stocks = [_stock("SZ000001")]
    stocks[0]["name"] = "TCL科技"
    payload = _load(build_search_index(_inputs(stocks), tmp_path))

    assert payload["entries"][0]["py"] == "TCLkj"  # 非汉字原样保留


def test_search_index_degrades_without_pypinyin(tmp_path: Path, monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "pypinyin", None)  # 模拟未安装 → import 抛 ImportError
    stocks = [_stock("SH600519")]
    stocks[0]["name"] = "贵州茅台"
    payload = _load(build_search_index(_inputs(stocks), tmp_path))

    assert payload["entries"][0]["py"] == ""  # 降级为空串，不阻塞跑批
