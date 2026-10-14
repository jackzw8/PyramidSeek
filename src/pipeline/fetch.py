"""采集编排（占位，任务 M1-6）。

组装：主源取数 → 失败回退官方源 → 再失败回退 ``fixtures/``（家法 R16：离线永远可跑）。
"""

from __future__ import annotations

from typing import Any


def fetch_universe(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """采集全市场标准层数据（未实现）。"""
    raise NotImplementedError("fetch_universe 待 M1-6 实现")


__all__ = ["fetch_universe"]
