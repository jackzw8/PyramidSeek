"""落库（任务 M1-5；依据《数据字典》§1 分层、§5 建表 DDL）。

分层（《数据字典》§1）：``原始层 raw/`` → ``标准层 standard/`` → ``分析层 analysis/`` → 产物层。

本模块负责 **标准层落库**：DuckDB（本地分析库）+ Parquet（冷存）。
- **原始返回缓存**由数据源层负责（``data/raw/<源>/<日期>/``），此处不重复实现；
- **分析层**（评分结果 ``score_snapshot``）在 M2 由计算层写入，DDL 已就位。

幂等（家法 R19）：建表用 ``CREATE TABLE IF NOT EXISTS``，写入用 ``INSERT OR REPLACE``
（主键来自 DDL），因此**重复执行结果一致**，可安全重跑。

DDL 来源：
- ``stock`` / ``financial_report`` / ``price_daily`` / ``score_snapshot`` —— **逐字取自
  《数据字典》§5**；
- ``industry_node`` / ``meta_snapshot`` —— §5 未给 DDL，按 §3.4 / §3.6 的字段定义推导
  （见《计划完成情况》D-13）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .. import config
from ..datasource.akshare_src import is_missing

logger = logging.getLogger(__name__)

# ── 建表 DDL（§5 逐字；后两张按 §3.4 / §3.6 推导）─────────────────────────
DDL: dict[str, str] = {
    "stock": """
        CREATE TABLE IF NOT EXISTS stock (
          code VARCHAR PRIMARY KEY, name VARCHAR, market VARCHAR, exchange VARCHAR,
          l1_code VARCHAR, l1_name VARCHAR, l2_code VARCHAR, l2_name VARCHAR,
          industry_std VARCHAR, listed_at DATE, shares DECIMAL(18,4),
          status VARCHAR, is_financial BOOLEAN, updated_at TIMESTAMP
        )""",
    "financial_report": """
        CREATE TABLE IF NOT EXISTS financial_report (
          code VARCHAR, report_period DATE, report_type VARCHAR, is_cumulative BOOLEAN,
          revenue DECIMAL(18,4), gross_profit DECIMAL(18,4), net_profit DECIMAL(18,4),
          total_assets DECIMAL(18,4), total_liabilities DECIMAL(18,4), equity DECIMAL(18,4),
          cash DECIMAL(18,4), op_cashflow DECIMAL(18,4), icf DECIMAL(18,4), fcf DECIMAL(18,4),
          effective_date DATE, currency VARCHAR, source VARCHAR, fetched_at TIMESTAMP,
          PRIMARY KEY (code, report_period, report_type)
        )""",
    "price_daily": """
        CREATE TABLE IF NOT EXISTS price_daily (
          code VARCHAR, trade_date DATE, open DECIMAL(18,4), high DECIMAL(18,4),
          low DECIMAL(18,4), close DECIMAL(18,4), volume BIGINT,
          adj_factor DECIMAL(18,6), close_qfq DECIMAL(18,4),
          PRIMARY KEY (code, trade_date)
        )""",
    "score_snapshot": """
        CREATE TABLE IF NOT EXISTS score_snapshot (
          code VARCHAR, snapshot_version VARCHAR,
          quality DECIMAL(10,2), growth DECIMAL(10,2),
          growth_np DECIMAL(10,2), growth_ocf DECIMAL(10,2),
          score DECIMAL(10,2),
          sub_roe DECIMAL(6,2), sub_net_profit_scale DECIMAL(6,2), sub_roa DECIMAL(6,2),
          sub_gross_margin DECIMAL(6,2), sub_cash_ratio DECIMAL(6,2),
          sub_asset_turnover DECIMAL(6,2),
          radar_roe DECIMAL(6,2), radar_gross_margin DECIMAL(6,2),
          radar_asset_turnover DECIMAL(6,2), radar_eps DECIMAL(6,2),
          radar_cash_ratio DECIMAL(6,2), radar_net_margin DECIMAL(6,2),
          compare_level VARCHAR, compare_group VARCHAR, rank_in_group INT, is_top10pct BOOLEAN,
          PRIMARY KEY (code, snapshot_version)
        )""",
    # ── 以下两张 §5 未给 DDL，按 §3.4 / §3.6 字段定义推导（D-13）──────────
    "industry_node": """
        CREATE TABLE IF NOT EXISTS industry_node (
          standard VARCHAR, version VARCHAR, level INT,
          node_code VARCHAR, node_name VARCHAR, parent_code VARCHAR, member_count INT,
          PRIMARY KEY (standard, version, level, node_code)
        )""",
    "meta_snapshot": """
        CREATE TABLE IF NOT EXISTS meta_snapshot (
          snapshot_version VARCHAR PRIMARY KEY,
          data_date_financial DATE, data_date_price DATE,
          industry_std VARCHAR, formula_version VARCHAR,
          degraded BOOLEAN, notes VARCHAR
        )""",
}

# 各表写入列（顺序即 DDL 顺序）
COLUMNS: dict[str, tuple[str, ...]] = {
    "stock": (
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
    "financial_report": (
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
    "price_daily": (
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
}
# 日期 / 时间戳列（需要在写入前归一格式）
DATE_COLUMNS = ("listed_at", "report_period", "effective_date", "trade_date")
TIMESTAMP_COLUMNS = ("updated_at", "fetched_at")


@dataclass
class StoreStats:
    """落库统计（M1-6 编排据此汇总日志；幂等重跑时各计数相同）。"""

    path: Path
    rows: dict[str, int]
    parquet: list[Path]


def _connect(db_path: Path) -> Any:
    """打开（必要时创建）DuckDB 连接。

    ``duckdb`` 延迟导入（与数据源层一致）：未装该库时不影响其他模块 import。
    """
    try:
        import duckdb  # noqa: PLC0415  （刻意延迟导入）
    except ImportError as exc:  # pragma: no cover - 依赖已在 requirements.txt 声明
        raise RuntimeError("duckdb 未安装：pip install duckdb") from exc
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def init_schema(conn: Any) -> None:
    """建表（幂等）。"""
    for table, statement in DDL.items():
        conn.execute(statement)
        logger.debug("已确保表存在：%s", table)


def to_db_date(value: Any) -> str | None:
    """日期归一为 ``YYYY-MM-DD``；空值返回 ``None``（家法 R11）。"""
    if is_missing(value):
        return None
    text = str(value)[:10].replace("/", "-")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        logger.warning("无法解析日期，按空值处理：%r", value)
        return None


def to_db_timestamp(value: Any) -> str | None:
    """时间戳归一为 ``YYYY-MM-DD HH:MM:SS``。

    数据源给的是 UTC ISO8601（可能带 ``Z``），DuckDB 的 ``TIMESTAMP`` 不认尾缀 ``Z``，
    若原样写入会报错或写成 NULL，故在此统一去掉时区标记。
    """
    if is_missing(value):
        return None
    text = str(value).strip().replace("T", " ")[:19]
    try:
        datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.warning("无法解析时间戳，按空值处理：%r", value)
        return None
    return text


def _prepare(table: str, row: dict[str, Any]) -> tuple[Any, ...]:
    """按 DDL 列顺序取値，并归一日期/时间戳。"""
    values: list[Any] = []
    for column in COLUMNS[table]:
        value = row.get(column)
        if column in DATE_COLUMNS:
            value = to_db_date(value)
        elif column in TIMESTAMP_COLUMNS:
            value = to_db_timestamp(value)
        values.append(value)
    return tuple(values)


def upsert(conn: Any, table: str, rows: list[dict[str, Any]]) -> int:
    """按主键写入（``INSERT OR REPLACE``）→ 幂等；返回写入行数。"""
    if not rows:
        return 0
    columns = COLUMNS[table]
    placeholders = ", ".join("?" for _ in columns)
    statement = f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    conn.executemany(statement, [_prepare(table, row) for row in rows])
    logger.info("写入 %s：%d 行", table, len(rows))
    return len(rows)


def export_parquet(conn: Any, table: str, parquet_dir: Path) -> Path:
    """把表导出为 Parquet（冷存）；已存在则覆盖，保证幂等。"""
    parquet_dir.mkdir(parents=True, exist_ok=True)
    path = parquet_dir / f"{table}.parquet"
    conn.execute(f"COPY (SELECT * FROM {table}) TO '{path.as_posix()}' (FORMAT PARQUET)")
    return path


def write_standard(
    result: Any,
    *,
    db_path: Path | None = None,
    parquet_dir: Path | None = None,
    export: bool = True,
) -> StoreStats:
    """把清洗结果（``pipeline.clean.CleanResult``）落入标准层。

    :param result: 形如 ``CleanResult``（需有 ``stocks`` / ``financials`` / ``prices`` 属性）。
    :param db_path: DuckDB 文件路径，默认 ``data/standard.duckdb``。
    :param parquet_dir: Parquet 目录，默认 ``data/parquet``。
    :param export: 是否导出 Parquet。
    """
    db_path = Path(db_path) if db_path else config.DATA_DIR / "standard.duckdb"
    parquet_dir = Path(parquet_dir) if parquet_dir else config.DATA_DIR / "parquet"

    financials: list[dict[str, Any]] = [
        row for rows in getattr(result, "financials", {}).values() for row in rows
    ]
    prices: list[dict[str, Any]] = [
        row for rows in getattr(result, "prices", {}).values() for row in rows
    ]

    conn = _connect(db_path)
    try:
        init_schema(conn)
        counts = {
            "stock": upsert(conn, "stock", list(getattr(result, "stocks", []))),
            "financial_report": upsert(conn, "financial_report", financials),
            "price_daily": upsert(conn, "price_daily", prices),
        }
        written: list[Path] = []
        if export:
            for table in ("stock", "financial_report", "price_daily"):
                written.append(export_parquet(conn, table, parquet_dir))
    finally:
        conn.close()

    logger.info("标准层落库完成：%s（%s）", db_path, counts)
    return StoreStats(path=db_path, rows=counts, parquet=written)


def read_table(table: str, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    """读回整张表（便于 M1-6 校验与排查）。"""
    db_path = Path(db_path) if db_path else config.DATA_DIR / "standard.duckdb"
    conn = _connect(db_path)
    try:
        init_schema(conn)
        cursor = conn.execute(f"SELECT * FROM {table}")
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        conn.close()


def table_columns(table: str, *, db_path: Path | None = None) -> list[str]:
    """取表列名（按 DDL 顺序）——用于断言「表结构与《数据字典》一致」。"""
    db_path = Path(db_path) if db_path else config.DATA_DIR / "standard.duckdb"
    conn = _connect(db_path)
    try:
        init_schema(conn)
        return [row[1] for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()]
    finally:
        conn.close()


def snapshot_stamp() -> str:
    """当前 UTC 时间戳（``YYYY-MM-DD HH:MM:SS``），供 meta_snapshot / 产物使用。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "COLUMNS",
    "DATE_COLUMNS",
    "DDL",
    "TIMESTAMP_COLUMNS",
    "StoreStats",
    "export_parquet",
    "init_schema",
    "read_table",
    "snapshot_stamp",
    "table_columns",
    "to_db_date",
    "to_db_timestamp",
    "upsert",
    "write_standard",
]
