"""跑批总入口（M3-4）的端到端测试：离线 fixtures 全链 + 落库 + 幂等。"""

from __future__ import annotations

import json
from pathlib import Path

from src import batch, config
from src.datasource.fixtures_src import FixturesSource
from src.pipeline import fetch, store

FIXED_AT = "2026-10-15T00:00:00Z"
FIXED_SNAPSHOT = "2026-10-15"


def _run_offline(out_dir: Path, *extra: str) -> int:
    return batch.main(
        [
            "--offline",
            "--out",
            str(out_dir),
            "--generated-at",
            FIXED_AT,
            *extra,
        ]
    )


def test_offline_batch_produces_full_products(tmp_path: Path) -> None:
    out = tmp_path / "data"

    assert _run_offline(out, "--no-store") == 0

    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["snapshotVersion"] == FIXED_SNAPSHOT
    assert meta["contractVersion"] == config.CONTRACT_VERSION
    assert meta["dataDate"]["financial"] == "2024-12-31"
    assert "dataDate" in meta and "price" not in meta["dataDate"]  # fixtures 无行情
    assert meta["degraded"] is True  # fixtures 非主源
    assert config.DISCLAIMER in meta["notes"]

    industries = json.loads((out / "industries.json").read_text(encoding="utf-8"))
    assert industries["markets"] == ["A", "HK"]
    assert industries["counts"]

    for market in config.MARKETS:
        ranking = json.loads((out / "ranking" / f"{market}.json").read_text(encoding="utf-8"))
        assert ranking["market"] == market
        assert ranking["rows"], f"{market} 主榜不应为空"
        assert ranking["excluded"], f"{market} 应有单列（ST / suspended / new）"

    stock_files = sorted((out / "stock").glob("*.json"))
    assert len(stock_files) == 12  # fixtures 全量：A 8 + HK 4

    index = json.loads((out / "search-index.json").read_text(encoding="utf-8"))
    assert len(index["entries"]) == 12
    assert {entry["code"] for entry in index["entries"]} == {path.stem for path in stock_files}
    assert index["entries"][0]["py"]  # 已装 pypinyin → 拼音非空
    assert {entry["market"] for entry in index["entries"]} == {"A", "HK"}


def test_offline_batch_is_idempotent(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"

    assert _run_offline(first, "--no-store") == 0
    assert _run_offline(second, "--no-store") == 0

    rel = sorted(path.relative_to(first) for path in first.rglob("*.json"))
    assert rel
    for path in rel:
        assert (first / path).read_bytes() == (second / path).read_bytes(), path


def test_batch_writes_analytics_layer(tmp_path: Path) -> None:
    out = tmp_path / "data"
    db_path = tmp_path / "standard.duckdb"

    assert _run_offline(out, "--db", str(db_path)) == 0

    scores = store.read_table("score_snapshot", db_path=db_path)
    assert len(scores) == 12
    assert {row["snapshot_version"] for row in scores} == {FIXED_SNAPSHOT}
    assert all(row["compare_group"] for row in scores)

    meta_rows = store.read_table("meta_snapshot", db_path=db_path)
    assert len(meta_rows) == 1
    assert str(meta_rows[0]["data_date_financial"]) == "2024-12-31"  # DuckDB DATE → date 对象
    assert meta_rows[0]["data_date_price"] is None  # fixtures 无行情
    assert meta_rows[0]["formula_version"] == config.FORMULA_VERSION


def test_assemble_inputs_carries_dates_degrade_and_scores() -> None:
    outcome = fetch.fetch_universe(markets=("A",), chain=[FixturesSource()], persist=False)

    inputs = batch.assemble_inputs(
        outcome,
        snapshot_version=FIXED_SNAPSHOT,
        generated_at=FIXED_AT,
        as_of=FIXED_SNAPSHOT,
    )

    assert len(inputs.stocks) == 8  # 只取 A 股
    assert len(inputs.scored) == 8
    assert inputs.data_date_financial == "2024-12-31"
    assert inputs.data_date_price is None
    assert inputs.degraded is True
    assert any("降级" in note for note in inputs.notes)


def test_batch_removes_stale_products(tmp_path: Path) -> None:
    """跑批前重置产物目录：上一批的分片文件不得残留（否则成孤儿，引用完整性失败）。"""
    out = tmp_path / "data"
    (out / "stock").mkdir(parents=True)
    (out / "ranking").mkdir(parents=True)
    (out / "stock" / "SZ999999.json").write_text("{}", encoding="utf-8")
    (out / "ranking" / "US.json").write_text("{}", encoding="utf-8")

    assert _run_offline(out, "--no-store") == 0

    assert not (out / "stock" / "SZ999999.json").exists()
    assert not (out / "ranking" / "US.json").exists()
    assert (out / "ranking" / "A.json").exists()


def test_unknown_market_is_rejected(tmp_path: Path) -> None:
    assert _run_offline(tmp_path / "data", "--no-store", "--markets", "US") == 2


def test_missing_fixtures_is_rejected(tmp_path: Path) -> None:
    code = batch.main(
        [
            "--offline",
            "--fixtures",
            str(tmp_path / "nope.json"),
            "--out",
            str(tmp_path / "data"),
        ]
    )

    assert code == 2
