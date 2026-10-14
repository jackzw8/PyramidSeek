"""个股详情产物（占位，任务 M3-2）。

一码一文件 ``stock/{code}.json``，结构遵循《产物契约》§3.4：
评分 / 子分 / 雷达六维 / 四张财务表 / 可选 ``kline`` / ``peers``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_stock(stock: dict[str, Any], peers: list[dict[str, Any]], out_dir: Path) -> Path:
    """生成个股详情产物（未实现）。"""
    raise NotImplementedError("build_stock 待 M3-2 实现")


__all__ = ["build_stock"]
