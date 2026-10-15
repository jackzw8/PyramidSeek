"""产物校验（M3-5）的测试：《产物契约》§6 的六类校验 + 非零退出。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src import config
from src.build.validate import main, validate_products


# ── 造一份"正确"的产物（手写，便于逐项篡改）──────────────────────────────
def _meta() -> dict[str, Any]:
    return {
        "snapshotVersion": "2026-10-15",
        "contractVersion": config.CONTRACT_VERSION,
        "formulaVersion": config.FORMULA_VERSION,
        "generatedAt": "2026-10-15T00:00:00Z",
        "dataDate": {"financial": "2024-12-31"},
        "industryStandard": {"A": "申万2021", "HK": "GICS"},
        "degraded": False,
        "notes": config.DISCLAIMER,
    }


def _ranking_row(rank: int, code: str) -> dict[str, Any]:
    return {
        "rank": rank,
        "code": code,
        "name": f"公司{code}",
        "market": "A",
        "l1": "公用事业",
        "l2": "电力",
        "group": "公用事业",
        "level": "L1",
        "score": 500.0,
        "quality": 450.0,
        "growth": 50.0,
        "roe": 0.05,
        "gross_margin": 0.4,
        "cash_ratio": 0.04,
        "debt_ratio": 0.6,
        "isTop10pct": True,
    }


def _stock_payload(code: str) -> dict[str, Any]:
    return {
        "code": code,
        "name": f"公司{code}",
        "market": "A",
        "exchange": "SSE",
        "l1": "公用事业",
        "l2": "电力",
        "group": "公用事业",
        "level": "L1",
        "industryStd": "申万2021",
        "listedAt": "2000-01-01",
        "score": 500.0,
        "quality": 450.0,
        "growth": 50.0,
        "growthDetail": {"netProfit": 30.0, "opCashflow": 20.0},
        "qualitySubs": {
            "roe": 50.0,
            "net_profit_scale": 60.0,
            "roa": 40.0,
            "gross_margin": 70.0,
            "cash_ratio": 30.0,
            "asset_turnover": 40.0,
        },
        "radar": {
            "roe": 50.0,
            "gross_margin": 70.0,
            "asset_turnover": 40.0,
            "eps": 20.0,
            "cash_ratio": 30.0,
            "net_margin": 80.0,
        },
        "years": ["2023", "2024"],
        "profitability": {"roe": [0.05, 0.05], "eps": [0.1, 0.1]},
        "structure": {"cash_ratio": [0.04, 0.04], "debt_ratio": [0.6, 0.6]},
        "operation": {"ar_turnover": [None, None], "asset_turnover": [0.2, 0.2]},
        "cashflow": {"cash": [20.0, 20.0], "ncf": [7.0, 7.0]},
        "peers": [{"code": code, "name": f"公司{code}", "roe5y": 0.05}],
    }


def _write_products(base: Path) -> Path:
    data = base / "data"
    (data / "ranking").mkdir(parents=True)
    (data / "stock").mkdir(parents=True)

    (data / "meta.json").write_text(json.dumps(_meta(), ensure_ascii=False), encoding="utf-8")
    (data / "industries.json").write_text(
        json.dumps(
            {
                "snapshotVersion": "2026-10-15",
                "contractVersion": config.CONTRACT_VERSION,
                "markets": ["A", "HK"],
                "tree": [],
                "counts": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data / "ranking" / "A.json").write_text(
        json.dumps(
            {
                "meta": {
                    "snapshotVersion": "2026-10-15",
                    "contractVersion": config.CONTRACT_VERSION,
                },
                "market": "A",
                "columns": list(_ranking_row(1, "SH600900")),
                "rows": [_ranking_row(1, "SH600900"), _ranking_row(2, "SH600011")],
                "excluded": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for code in ("SH600900", "SH600011"):
        (data / "stock" / f"{code}.json").write_text(
            json.dumps(_stock_payload(code), ensure_ascii=False), encoding="utf-8"
        )
    return data


@pytest.fixture()
def products(tmp_path: Path) -> Path:
    return _write_products(tmp_path)


def _mutate(path: Path, mutate) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── 用例 ─────────────────────────────────────────────────────────────────
def test_valid_products_pass(products: Path) -> None:
    assert validate_products(products) == []


def test_missing_stock_file_breaks_reference_integrity(products: Path) -> None:
    (products / "stock" / "SH600011.json").unlink()

    problems = validate_products(products)
    assert any("引用完整性" in problem and "SH600011" in problem for problem in problems)


def test_orphan_stock_file_is_reported(products: Path) -> None:
    (products / "stock" / "SZ999999.json").write_text(
        json.dumps(_stock_payload("SZ999999"), ensure_ascii=False), encoding="utf-8"
    )

    problems = validate_products(products)
    assert any("孤儿" in problem for problem in problems)


def test_rank_gap_is_reported(products: Path) -> None:
    _mutate(
        products / "ranking" / "A.json",
        lambda data: data["rows"][1].__setitem__("rank", 5),
    )

    assert any("rank" in problem for problem in validate_products(products))


def test_rows_excluded_overlap_is_reported(products: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        row = dict(data["rows"][1])
        for key in (
            "rank",
            "group",
            "level",
            "isTop10pct",
            "roe",
            "gross_margin",
            "cash_ratio",
            "debt_ratio",
        ):
            row.pop(key, None)
        row.update({"status": "ST", "reason": "ST"})
        data["excluded"] = [row]

    _mutate(products / "ranking" / "A.json", mutate)

    assert any("同时出现" in problem for problem in validate_products(products))


def test_out_of_range_score_fails_schema(products: Path) -> None:
    _mutate(
        products / "ranking" / "A.json",
        lambda data: data["rows"][0].__setitem__("score", 1200.0),
    )

    assert any("schema" in problem for problem in validate_products(products))


def test_out_of_range_subscore_is_reported(products: Path) -> None:
    _mutate(
        products / "stock" / "SH600900.json",
        lambda data: data["qualitySubs"].__setitem__("roe", 150.0),
    )

    assert any("超出 [0,100]" in problem for problem in validate_products(products))


def test_version_mismatch_is_reported(products: Path) -> None:
    _mutate(
        products / "ranking" / "A.json",
        lambda data: data["meta"].__setitem__("contractVersion", "9.9"),
    )

    assert any("contractVersion" in problem for problem in validate_products(products))


def test_table_length_mismatch_is_reported(products: Path) -> None:
    _mutate(
        products / "stock" / "SH600900.json",
        lambda data: data["profitability"].__setitem__("roe", [0.05]),
    )

    assert any("profitability.roe" in problem for problem in validate_products(products))


def test_code_filename_mismatch_is_reported(products: Path) -> None:
    _mutate(
        products / "stock" / "SH600900.json",
        lambda data: data.__setitem__("code", "SH600011"),
    )

    assert any("与文件名不一致" in problem for problem in validate_products(products))


def test_missing_meta_fails(products: Path) -> None:
    (products / "meta.json").unlink()

    assert any("缺少产物文件" in problem for problem in validate_products(products))


# ── CLI 退出码 ───────────────────────────────────────────────────────────
def test_main_returns_zero_on_success(products: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["--data", str(products)]) == 0


def test_main_returns_nonzero_on_failure(products: Path) -> None:
    (products / "stock" / "SH600011.json").unlink()

    assert main(["--data", str(products)]) == 1
