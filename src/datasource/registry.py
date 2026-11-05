"""财报源注册表（M5-1 / D-55 / ADR-015，2026-11-04）。

**平权可选源**：按市场分组，跑批时每组选一个；**无主备链、失败不自动换源**。

| 市场 | 可选源 | 默认 |
|---|---|---|
| A 股 | ``sina``（新浪）/ ``em``（东财）/ ``ths``（同花顺） | ``em`` |
| 港股 | ``tx``（腾讯）/ ``em``（东财） | ``em`` |

**腾讯 A 股不在表内** —— 实测无可用的结构化财报接口（旧页 302、bundle 无三表 API、
akshare 无封装），见 D-55。

所有源继承 :class:`AkShareSource`，只覆写 ``financials``；列表 / 行情 / 快照 / 行业树
是全市场级接口，与财报选源无关，统一复用新浪系实现。
"""

from __future__ import annotations

from typing import Any

from .akshare_src import AkShareSource, AkShareThsSource
from .em_src import EastMoneySource
from .tx_hk_src import TencentHKSource

__all__ = [
    "DEFAULT_SOURCE",
    "SOURCES",
    "available_sources",
    "build_source",
    "source_label",
    "validate_selection",
]

# market → name → (中文标签, 实现类)
SOURCES: dict[str, dict[str, tuple[str, type]]] = {
    "A": {
        "sina": ("新浪（AkShare）", AkShareSource),
        "em": ("东方财富", EastMoneySource),
        "ths": ("同花顺 THS", AkShareThsSource),
    },
    "HK": {
        "tx": ("腾讯", TencentHKSource),
        "em": ("东方财富", EastMoneySource),
    },
}

# D-67：两个默认源都自带「基本每股收益」，无需为 EPS 反算股本
DEFAULT_SOURCE: dict[str, str] = {"A": "sina", "HK": "tx"}


def available_sources(market: str) -> list[str]:
    """某市场可选源名（注册表顺序即控制台下拉顺序）。"""
    if market not in SOURCES:
        raise ValueError(f"不支持的市场：{market!r}（应为 {tuple(SOURCES)} 之一）")
    return list(SOURCES[market])


def source_label(market: str, name: str) -> str:
    """源中文标签；未知源名抛错（宁可显式失败，也不静默回落到默认）。"""
    entry = SOURCES.get(market, {}).get(name)
    if entry is None:
        raise ValueError(f"市场 {market} 无可用源 {name!r}；可选：{available_sources(market)}")
    return entry[0]


def build_source(market: str, name: str, **kwargs: Any) -> Any:
    """实例化所选源；``kwargs`` 透传给构造函数（``partition`` / ``stale_after`` 等）。"""
    entry = SOURCES.get(market, {}).get(name)
    if entry is None:
        raise ValueError(f"市场 {market} 无可用源 {name!r}；可选：{available_sources(market)}")
    return entry[1](**kwargs)


def validate_selection(selection: dict[str, str] | None) -> dict[str, str]:
    """校验 + 回填默认值，返回 ``{market: source}`` 的完整选择。

    只选了 A 股时港股自动用默认值；未知市场或未知源名一律报错（不静默忽略）。
    """
    result = dict(DEFAULT_SOURCE)
    for market, name in (selection or {}).items():
        if market not in SOURCES:
            raise ValueError(f"未知市场 {market!r}；可选：{tuple(SOURCES)}")
        if name not in SOURCES[market]:
            raise ValueError(f"市场 {market} 无可用源 {name!r}；可选：{available_sources(market)}")
        result[market] = name
    return result
