"""AkShare 数据源实现（占位，任务 M1-2）。

要点（M1-2 实现时）：
- 重试 3 次 + 指数退避(1s/3s/9s) + 超时 15s（参数取自 ``config``）；
- 本地文件缓存（原始返回，按日期），便于复现与排查接口变动；
- 断网 / 限流时抛出 ``DataSourceError``，由上层回退 fixtures 或官方源。
"""

from __future__ import annotations

from typing import Any

from .base import DataSourceError


class AkShareSource:
    """AkShare 主源（未实现）。"""

    name = "akshare"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("AkShareSource 待 M1-2 实现")

    def list_stocks(self, market: str) -> list[dict[str, Any]]:
        raise NotImplementedError("M1-2")

    def financials(self, code: str) -> list[dict[str, Any]]:
        raise NotImplementedError("M1-2")

    def price_daily(self, code: str, adjust: str = "qfq") -> list[dict[str, Any]]:
        raise NotImplementedError("M1-2")

    def industry_tree(self, market: str) -> list[dict[str, Any]]:
        raise NotImplementedError("M1-2")

    def health_check(self) -> bool:
        raise NotImplementedError("M1-2")


__all__ = ["AkShareSource", "DataSourceError"]
