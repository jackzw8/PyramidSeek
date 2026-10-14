"""行业分位与对比池口径（占位，任务 M2-4）。

规则（《评分算法规格》§5 / 家法 R5、R6）：
- 默认二级；二级样本 < ``config.INDUSTRY_FALLBACK_MIN``（30）回退一级；
- 分位分 = 跑赢同业比例 × 100，并列值取半数（``(less + 0.5·eq) / n × 100``）；
- 单股池（n ≤ 1）返回 50；分市场独立，不跨市场混排；概念板块不纳入。
"""

from __future__ import annotations

from typing import Any


def percentile_score(values: list[float], value: float) -> float:
    """行业分位子分（未实现）。"""
    raise NotImplementedError("percentile_score 待 M2-4 实现")


def choose_level(
    stocks: list[dict[str, Any]], l2: str, l1: str
) -> tuple[str, str, list[dict[str, Any]]]:
    """选对比池：返回 (level, group, members)（未实现）。"""
    raise NotImplementedError("choose_level 待 M2-4 实现")


__all__ = ["choose_level", "percentile_score"]
