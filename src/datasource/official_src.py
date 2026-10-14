"""官方源兜底实现（占位，任务 M1-3）。

对账 / 补数用途：
- A股：巨潮资讯（cninfo）公告解析三表；
- 港股：港交所披露易（hkexnews）公告解析；
- 按需触发（主源失败或抽样校对），不做全量高频抓取；遵守目标站点抓取规范。
"""

from __future__ import annotations

from typing import Any


class OfficialSource:
    """官方源兜底（未实现）。"""

    name = "official"

    def __init__(self, market: str = "A", *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("OfficialSource 待 M1-3 实现")

    def list_stocks(self, market: str) -> list[dict[str, Any]]:
        raise NotImplementedError("M1-3")

    def financials(self, code: str) -> list[dict[str, Any]]:
        raise NotImplementedError("M1-3")

    def price_daily(self, code: str, adjust: str = "qfq") -> list[dict[str, Any]]:
        raise NotImplementedError("M1-3")

    def industry_tree(self, market: str) -> list[dict[str, Any]]:
        raise NotImplementedError("M1-3")

    def health_check(self) -> bool:
        raise NotImplementedError("M1-3")


__all__ = ["OfficialSource"]
