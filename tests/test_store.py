"""落库的离线测试（任务 M1-5）。

用真实 DuckDB 写到 ``tmp_path``（本地分析库本来就是嵌入式，无需 mock）。
重点覆盖：**表结构与《数据字典》一致**（schema 一旦漂移后面全错）、**幂等**、Parquet 冷存。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")  # duckdb 是声明依赖；缺了应显式跳过而不是崩

from src.pipeline import store  # noqa: E402


class CleanResultStub:
    """只需要 clean.CleanResult 的三个属性（避免测试跨模块耦合）。"""

    def __init__(self, stocks=(), financials=None, prices=None) -> None:
        self.stocks = list(stocks)
        self.financials = financials or {}
        self.prices = prices or {}


def stock_row(**overrides) -> dict:
    row = {
        "code": "SH600900",
        "name": "长江电力",
        "market": "A",
        "exchange": "SSE",
        "l1_code": "801160",
        "l1_name": "公用事业",
        "l2_code": "801161",
        "l2_name": "电力",
        "industry_std": "申万2021",
        "listed_at": "2003-11-18",
        "shares": 244.70,
        "status": "normal",
        "is_financial": False,
        "updated_at": "2026-10-14T00:00:00Z",
    }
    row.update(overrides)
    return row


def financial_row(**overrides) -> dict:
    row = {
        "code": "SH600900",
        "report_period": "2024-12-31",
        "report_type": "annual",
        "is_cumulative": False,
        "revenue": 845.0,
        "gross_profit": 524.0,
        "net_profit": 325.0,
        "total_assets": 6200.0,
        "total_liabilities": 3520.0,
        "equity": 2680.0,
        "cash": 90.0,
        "op_cashflow": 596.0,
        "icf": -450.0,
        "fcf": -100.0,
        "effective_date": "2025-04-30",
        "source": "fixture",
        "fetched_at": "2026-10-14T00:00:00Z",
    }
    row.update(overrides)
    return row


def price_row(**overrides) -> dict:
    row = {
        "code": "SH600900",
        "trade_date": "2024-06-28",
        "open": 24.0,
        "high": 25.5,
        "low": 23.8,
        "close": 25.0,
        "volume": 123456,
        "adj_factor": 0.98,
        "close_qfq": 24.5,
    }
    row.update(overrides)
    return row


def make_result(**overrides) -> CleanResultStub:
    data = {
        "stocks": [stock_row()],
        "financials": {"SH600900": [financial_row()]},
        "prices": {"SH600900": [price_row()]},
    }
    data.update(overrides)
    return CleanResultStub(**data)


# ── 表结构（schema 与《数据字典》对齐）────────────────────────────────────
def test_schema_has_six_tables(tmp_path) -> None:
    db_path = tmp_path / "standard.duckdb"
    store.write_standard(make_result(), db_path=db_path, export=False)

    conn = duckdb.connect(str(db_path))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert names == set(store.DDL)


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        (
            "stock",
            (
                "code",
                "name",
                "market",
                "exchange",
                "l1_code",
                "l1_name",
                "l2_code",
                "l2_name",
                "industry_std",
                "listed_at",
                "shares",
                "status",
                "is_financial",
                "updated_at",
            ),
        ),
        (
            "financial_report",
            (
                "code",
                "report_period",
                "report_type",
                "is_cumulative",
                "revenue",
                "gross_profit",
                "net_profit",
                "total_assets",
                "total_liabilities",
                "equity",
                "cash",
                "op_cashflow",
                "icf",
                "fcf",
                "effective_date",
                "currency",
                "source",
                "fetched_at",
            ),
        ),
        (
            "price_daily",
            (
                "code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "adj_factor",
                "close_qfq",
            ),
        ),
        (
            "industry_node",
            (
                "standard",
                "version",
                "level",
                "node_code",
                "node_name",
                "parent_code",
                "member_count",
            ),
        ),
        (
            "meta_snapshot",
            (
                "snapshot_version",
                "data_date_financial",
                "data_date_price",
                "industry_std",
                "formula_version",
                "degraded",
                "notes",
            ),
        ),
    ],
)
def test_table_columns_match_data_dictionary(
    tmp_path, table: str, expected: tuple[str, ...]
) -> None:
    """列名与顺序必须与《数据字典》§5（或 §3.4 / §3.6 推导）一致。"""
    columns = store.table_columns(table, db_path=tmp_path / "standard.duckdb")
    assert tuple(columns) == expected


def test_write_columns_cover_ddl_columns() -> None:
    """码表 COLUMNS 与 DDL 不能脱节，否则插入会少列/错列。"""
    for table, columns in store.COLUMNS.items():
        assert len(set(columns)) == len(columns)  # 无重复列
        for column in columns:
            assert column in store.DDL[table]


# ── 写入 / 读回 ───────────────────────────────────────────────────────────
def test_write_and_read_round_trip(tmp_path) -> None:
    db_path = tmp_path / "standard.duckdb"
    stats = store.write_standard(make_result(), db_path=db_path, export=False)

    assert stats.path == db_path
    assert stats.rows == {"stock": 1, "financial_report": 1, "price_daily": 1}

    stocks = store.read_table("stock", db_path=db_path)
    assert stocks[0]["name"] == "长江电力"
    assert str(stocks[0]["listed_at"]) == "2003-11-18"
    assert stocks[0]["is_financial"] is False
    assert str(stocks[0]["updated_at"]) == "2026-10-14 00:00:00"  # 尾缀 Z 已归一

    financials = store.read_table("financial_report", db_path=db_path)
    assert float(financials[0]["net_profit"]) == 325.0
    assert str(financials[0]["effective_date"]) == "2025-04-30"

    prices = store.read_table("price_daily", db_path=db_path)
    assert float(prices[0]["adj_factor"]) == 0.98


def test_empty_input_still_creates_schema(tmp_path) -> None:
    db_path = tmp_path / "standard.duckdb"
    stats = store.write_standard(CleanResultStub(), db_path=db_path, export=False)

    assert stats.rows == {"stock": 0, "financial_report": 0, "price_daily": 0}
    assert store.read_table("stock", db_path=db_path) == []


# ── 幂等（家法 R19）──────────────────────────────────────────────────────
def test_rerun_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "standard.duckdb"
    first = store.write_standard(make_result(), db_path=db_path, export=False)
    second = store.write_standard(make_result(), db_path=db_path, export=False)

    assert first.rows == second.rows
    assert len(store.read_table("stock", db_path=db_path)) == 1
    assert len(store.read_table("financial_report", db_path=db_path)) == 1
    assert len(store.read_table("price_daily", db_path=db_path)) == 1


def test_rerun_replaces_instead_of_duplicating(tmp_path) -> None:
    db_path = tmp_path / "standard.duckdb"
    store.write_standard(make_result(), db_path=db_path, export=False)
    store.write_standard(
        make_result(stocks=[stock_row(name="长江电力（改名）")]), db_path=db_path, export=False
    )

    stocks = store.read_table("stock", db_path=db_path)
    assert len(stocks) == 1
    assert stocks[0]["name"] == "长江电力（改名）"


def test_composite_primary_key_of_financials(tmp_path) -> None:
    """financial_report 主键是 (code, report_period, report_type) —— 不同期各自成行。"""
    db_path = tmp_path / "standard.duckdb"
    result = make_result(
        financials={
            "SH600900": [
                financial_row(report_period="2023-12-31"),
                financial_row(report_period="2024-12-31"),
                financial_row(report_period="2024-12-31", report_type="h1", is_cumulative=True),
            ]
        }
    )
    store.write_standard(result, db_path=db_path, export=False)
    assert len(store.read_table("financial_report", db_path=db_path)) == 3


# ── 日期 / 时间戳归一 ────────────────────────────────────────────────────
def test_timestamp_and_date_normalisation(caplog) -> None:
    assert store.to_db_timestamp("2026-10-14T00:00:00Z") == "2026-10-14 00:00:00"
    assert store.to_db_timestamp("2026-10-14T12:34:56+08:00") == "2026-10-14 12:34:56"
    assert store.to_db_date("2024/12/31") == "2024-12-31"
    assert store.to_db_date(None) is None
    assert store.to_db_timestamp(None) is None

    with caplog.at_level(logging.WARNING, logger="src.pipeline.store"):
        assert store.to_db_date("不是日期") is None
    assert "无法解析日期" in caplog.text


def test_snapshot_stamp_format() -> None:
    stamp = store.snapshot_stamp()
    assert len(stamp) == 19
    assert stamp[4] == "-" and stamp[10] == " "


# ── Parquet 冷存 ─────────────────────────────────────────────────────────
def test_parquet_export_and_read_back(tmp_path) -> None:
    parquet_dir = tmp_path / "parquet"
    stats = store.write_standard(
        make_result(), db_path=tmp_path / "standard.duckdb", parquet_dir=parquet_dir
    )

    assert {path.name for path in stats.parquet} == {
        "stock.parquet",
        "financial_report.parquet",
        "price_daily.parquet",
    }
    assert all(Path(path).exists() for path in stats.parquet)

    conn = duckdb.connect()
    try:
        rows = conn.execute(
            f"SELECT code, name FROM read_parquet('{(parquet_dir / 'stock.parquet').as_posix()}')"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("SH600900", "长江电力")]


def test_parquet_export_is_overwritten_not_appended(tmp_path) -> None:
    parquet_dir = tmp_path / "parquet"
    store.write_standard(
        make_result(), db_path=tmp_path / "standard.duckdb", parquet_dir=parquet_dir
    )
    store.write_standard(
        make_result(), db_path=tmp_path / "standard.duckdb", parquet_dir=parquet_dir
    )

    conn = duckdb.connect()
    try:
        count = conn.execute(
            f"SELECT count(*) FROM read_parquet('{(parquet_dir / 'stock.parquet').as_posix()}')"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_default_paths_live_under_data_dir() -> None:
    """默认落在 ``data/``（已 gitignore），不污染仓库。"""
    assert store.config.DATA_DIR.name == "data"
    assert store.config.DATA_DIR.parent == store.config.PROJECT_ROOT


def test_score_snapshot_columns_include_radar_dimensions(tmp_path) -> None:
    """D-14 闭环：radar 六维已补进《数据字典》§5 的 score_snapshot DDL。"""
    columns = store.table_columns("score_snapshot", db_path=tmp_path / "standard.duckdb")
    assert tuple(columns) == (
        "code",
        "snapshot_version",
        "quality",
        "growth",
        "growth_np",
        "growth_ocf",
        "score",
        "sub_roe",
        "sub_net_profit_scale",
        "sub_roa",
        "sub_gross_margin",
        "sub_cash_ratio",
        "sub_asset_turnover",
        # 雷达六维：顺序即雷达图顶点顺序（契约 §3.4：顶 ROE 顺时针）
        "radar_roe",
        "radar_gross_margin",
        "radar_asset_turnover",
        "radar_eps",
        "radar_cash_ratio",
        "radar_net_margin",
        "compare_level",
        "compare_group",
        "rank_in_group",
        "is_top10pct",
    )
