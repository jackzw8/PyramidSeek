"""跑批历史（M5-4）：sqlite 记 run 元信息（``console/runs.db``）。

线程安全（Flask 多线程 + runner 后台线程都会写）；库文件小、单写者为主，
直接 ``threading.Lock`` + 短连接即可，无需连接池。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

_DB_PATH = Path(__file__).resolve().parent / "runs.db"
_LOCK = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    mode         TEXT NOT NULL,
    markets      TEXT,
    source_a     TEXT,
    source_hk    TEXT,
    codes_note   TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    exit_code    INTEGER,
    summary      TEXT
)
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def record_run(**fields: Any) -> None:
    """插入一条 running 记录（失败不应影响跑批本身 —— 历史是尽力而为）。"""
    with _LOCK:
        try:
            conn = _connect()
            _ensure(conn)
            conn.execute(
                "INSERT INTO runs (run_id, started_at, mode, markets, source_a, source_hk,"
                " codes_note, status) VALUES (?,?,?,?,?,?,?, 'running')",
                (
                    fields.get("run_id"),
                    fields.get("started_at"),
                    fields.get("mode"),
                    ",".join(fields.get("markets") or []),
                    fields.get("source_a"),
                    fields.get("source_hk"),
                    fields.get("codes_note"),
                ),
            )
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass


def finish_run(run_id: str, status: str, exit_code: int | None, summary: str = "") -> None:
    with _LOCK:
        try:
            conn = _connect()
            _ensure(conn)
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, exit_code=?, summary=? WHERE run_id=?",
                (
                    datetime_utc(),
                    status,
                    exit_code,
                    summary[:200],
                    run_id,
                ),
            )
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    with _LOCK:
        try:
            conn = _connect()
            _ensure(conn)
            rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []


def datetime_utc() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
