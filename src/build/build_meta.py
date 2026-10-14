"""元数据与行业树产物（占位，任务 M3-3）。

``meta.json``（快照版本 / 数据日期 / 分类版本 / 公式版本 / 降级标记）
与 ``industries.json``（市场-板块-行业树 + 各池样本数，含是否触发回退），
结构遵循《产物契约》§3.1、§3.2。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_meta(universe: list[dict[str, Any]], out_dir: Path, **kwargs: Any) -> Path:
    """生成 meta.json（未实现）。"""
    raise NotImplementedError("build_meta 待 M3-3 实现")


def build_industries(universe: list[dict[str, Any]], out_dir: Path, **kwargs: Any) -> Path:
    """生成 industries.json（未实现）。"""
    raise NotImplementedError("build_industries 待 M3-3 实现")


__all__ = ["build_industries", "build_meta"]
