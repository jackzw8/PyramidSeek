"""数据源层：统一接口 + 多实现（AkShare 主源 / 官方源兜底）。

接口契约（方法签名 / 参数口径 / 返回字段 / 异常约定）集中在 :mod:`.base`，
是 M1-1 的交付物；各实现见 :mod:`.akshare_src`（M1-2）、:mod:`.official_src`（M1-3）。
"""

from .base import (
    ADJUST_MODES,
    DEFAULT_ADJUST,
    FINANCIAL_OPTIONAL_FIELDS,
    FINANCIAL_REQUIRED_FIELDS,
    INDUSTRY_OPTIONAL_FIELDS,
    INDUSTRY_REQUIRED_FIELDS,
    MARKETS,
    PRICE_OPTIONAL_FIELDS,
    PRICE_REQUIRED_FIELDS,
    STOCK_OPTIONAL_FIELDS,
    STOCK_REQUIRED_FIELDS,
    DataSource,
    DataSourceError,
)

__all__ = [
    "ADJUST_MODES",
    "DEFAULT_ADJUST",
    "FINANCIAL_OPTIONAL_FIELDS",
    "FINANCIAL_REQUIRED_FIELDS",
    "INDUSTRY_OPTIONAL_FIELDS",
    "INDUSTRY_REQUIRED_FIELDS",
    "MARKETS",
    "PRICE_OPTIONAL_FIELDS",
    "PRICE_REQUIRED_FIELDS",
    "STOCK_OPTIONAL_FIELDS",
    "STOCK_REQUIRED_FIELDS",
    "DataSource",
    "DataSourceError",
]
