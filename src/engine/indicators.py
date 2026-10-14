"""指标计算（占位，任务 M2-1）。

8 项原始指标（《评分算法规格》§3）：roe / roa / gross_margin / net_margin /
cash_ratio / asset_turnover / net_profit_scale / eps，均取近 5 年**算术平均**，除零返回 0。

边界（单测必须覆盖）：负值（亏损）、零值（0 是有效值）、缺失值（``None``）。
"""

from __future__ import annotations

from typing import Any


def raw_indicators(stock: dict[str, Any], *, years: int = 5) -> dict[str, Any]:
    """计算单只股票的原始指标（未实现）。"""
    raise NotImplementedError("raw_indicators 待 M2-1 实现")


__all__ = ["raw_indicators"]
