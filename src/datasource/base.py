"""数据源抽象接口（《数据源接入规格》§2）。

调用约定：任何方法失败必须抛出可识别异常 ``DataSourceError``，
由上层（pipeline.fetch）决定是否回退到官方源或 fixtures。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class DataSourceError(RuntimeError):
    """数据源可识别异常：请求失败 / 超时 / 限流 / 接口改版。"""


@runtime_checkable
class DataSource(Protocol):
    """统一数据源接口，便于在 AkShare / 官方源 / 未来商业源之间替换。"""

    name: str

    def list_stocks(self, market: str) -> list[dict[str, Any]]:
        """股票列表 + 行业归属。"""
        ...

    def financials(self, code: str) -> list[dict[str, Any]]:
        """近 N 年财报（标准化字段见《数据字典》§3.2）。"""
        ...

    def price_daily(self, code: str, adjust: str = "qfq") -> list[dict[str, Any]]:
        """日行情；默认前复权（家法 R8）。"""
        ...

    def industry_tree(self, market: str) -> list[dict[str, Any]]:
        """行业分类树。"""
        ...

    def health_check(self) -> bool:
        """连通性自检。"""
        ...
