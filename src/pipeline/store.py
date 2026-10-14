"""落库（占位，任务 M1-5）。

要点（M1-5 实现时）：
- DuckDB（标准层 + 分析层）+ Parquet（冷存）+ 原始返回文件缓存；
- 建表 DDL 见《数据字典》§5；
- 可重复运行**幂等**（家法 R19）。
"""

from __future__ import annotations

from typing import Any


def write_standard(universe: list[dict[str, Any]], *, db_path: str | None = None) -> str:
    """写入标准层并返回库路径（未实现）。"""
    raise NotImplementedError("write_standard 待 M1-5 实现")


__all__ = ["write_standard"]
