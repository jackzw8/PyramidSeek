"""排行榜产物（占位，任务 M3-1）。

按市场分片输出 ``ranking/A.json``、``ranking/HK.json``，结构遵循
《产物契约》§3.3（contractVersion 1.1），列顺序由 ``columns`` 声明。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_ranking(scored: list[dict[str, Any]], market: str, out_dir: Path) -> Path:
    """生成单市场排行榜产物（未实现）。"""
    raise NotImplementedError("build_ranking 待 M3-1 实现")


__all__ = ["build_ranking"]
