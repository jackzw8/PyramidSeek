"""数据源层：统一接口 + 多实现（AkShare 主源 / 官方源兜底）。"""

from .base import DataSource, DataSourceError

__all__ = ["DataSource", "DataSourceError"]
