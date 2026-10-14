"""行业对比池与行业分位（任务 M2-4）。

- **对比池**（《评分算法规格》§5）：默认**二级行业**；二级样本 < ``config.INDUSTRY_FALLBACK_MIN``
  时回退到一级；**A 股 / 港股各自内部，不跨市场**；分组键用 ``l1_name`` / ``l2_name``
  （A 股为申万口径、港股为 GICS 口径，字段相同、标准不同）。
- **分位子分**（§4.2 方式二）：``子分 = 跑赢同业比例 × 100``，池内**包含自己**；
  ``n ≤ 1`` → 50。**子分不取整**（取整只在 M2-5 组装 Q；口径同 D-18）。
- **D-19 裁定**（用户 2026-10-14 定）：与 §8「负值 → 规模分取 0」冲突时以 §8 为准 ——
  **该股自己**的 ``net_profit_scale`` 为负 ⇒ 其子分强制 ``0``；但**对比池不排除负值成员**
  （否则池子变形、分位基准失真）。该规则只施加于 :data:`NEGATIVE_FORCES_ZERO` 里的指标。
- **未分类**（``config.INDUSTRY_UNCLASSIFIED``）不参与行业分位（config 注释的既定口径）：
  这类股票**不出现在** :func:`build_pools` 的结果里，上层用 :func:`unclassified_codes` 标注。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .. import config
from ..common import is_missing

# 值为负时子分强制 0 的指标（§8 只点名"规模分"；D-19 只施加于此）
NEGATIVE_FORCES_ZERO: tuple[str, ...] = ("net_profit_scale",)


@dataclass(frozen=True)
class PoolInfo:
    """一只股票所属的对比池。

    三处用得上：分位计算、产物 ``compare_level`` / ``compare_group``、以及排查。
    """

    market: str
    level: str  # "L1" / "L2"
    group: str  # 组名（产物 compare_group）
    size: int  # 池内**样本数**（按成员数计，不含未分类；不是"有值样本数"）
    use_fallback: bool
    members: tuple[str, ...]  # 池内全部 code（升序）


def _is_unclassified(stock: dict[str, Any]) -> bool:
    """行业未分类（任一层级为「未分类」即视为未分类）：不参与分位。"""
    return config.INDUSTRY_UNCLASSIFIED in (
        stock.get("l1_name"),
        stock.get("l2_name"),
    )


def _group_codes(stocks: Iterable[dict[str, Any]], field: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for stock in stocks:
        name = stock.get(field)
        if is_missing(name):
            continue
        groups.setdefault(str(name), []).append(str(stock.get("code")))
    return groups


def unclassified_codes(stocks: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """行业未分类的 code（升序）—— 供上层标注"不参与行业分位"。"""
    return tuple(sorted(str(stock.get("code")) for stock in stocks if _is_unclassified(stock)))


def build_pools(
    stocks: Iterable[dict[str, Any]],
    *,
    min_size: int = config.INDUSTRY_FALLBACK_MIN,
) -> dict[str, PoolInfo]:
    """按 §5 构建每只股票的对比池。

    - 回退判据用**成员数**（``len(二级成员) < min_size``），与 §5「二级样本 < 30」一致；
      **恰好 30 不回退**（``< min_size`` 而非 ``<=``）。
    - 一级池若也不足 ``min_size`` **不再继续回退**：§5 只定义了两级，
      再往上就只剩"全市场"，那已经不是"同业对比"了（保持 L1，由上层决定是否标注）。
    """
    pools: dict[str, PoolInfo] = {}
    for market in config.MARKETS:
        members = [
            stock
            for stock in stocks
            if stock.get("market") == market and not _is_unclassified(stock)
        ]
        level2 = _group_codes(members, "l2_name")
        level1 = _group_codes(members, "l1_name")

        for stock in members:
            code = str(stock.get("code"))
            name2 = str(stock.get("l2_name"))
            name1 = str(stock.get("l1_name"))
            group2 = level2.get(name2, [])
            use_fallback = len(group2) < min_size
            selected = level1.get(name1, []) if use_fallback else group2
            pools[code] = PoolInfo(
                market=market,
                level=(
                    config.COMPARE_LEVEL_FALLBACK if use_fallback else config.COMPARE_LEVEL_DEFAULT
                ),
                group=name1 if use_fallback else name2,
                size=len(selected),
                use_fallback=use_fallback,
                members=tuple(sorted(selected)),
            )
    return pools


def percentile_score(values: Sequence[float], value: float) -> float:
    """§4.2 方式二：``子分 = ((少于 + 0.5 × 并列) ÷ n) × 100``（池内**含自己**）。

    ``n ≤ 1`` → ``50``（§4.2 明文：样本只有一个时无从比较，给中位分）。
    全池同值 ⇒ 全部并列 ⇒ 每人 50 分。
    """
    n = len(values)
    if n <= 1:
        return 50.0
    less = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return ((less + 0.5 * equal) / n) * config.PERCENTILE_SCALE


def pool_sub_scores(
    indicator: str,
    values: dict[str, Any],
    pools: dict[str, PoolInfo],
) -> dict[str, float | None]:
    """按对比池算某"走分位"指标的全体子分（§4.2 方式二 + D-19）。

    :param indicator: 必须是 :data:`config.PERCENTILE_INDICATORS` 之一。
    :param values: ``code → 该指标值``；``None`` / ``NaN`` 视为无值。
    :returns: ``code → 子分``；**``None`` 表示"该股此项无值"**（不是 0 分！按 §8 应由上层
        用行业均值填充或标注不入榜，家法 R11）；**未分类的 code 不在结果里**（未进 pools）。

    池内成员一律基于 :func:`build_pools` 的 ``members`` 取齐（同一池共享一份 values），
    因此 **缺失值不参与分位基准**，但**负值参与**（D-19：只让负值成员自己得 0 分）。
    """
    if indicator not in config.PERCENTILE_INDICATORS:
        raise KeyError(f"{indicator!r} 不走行业分位（应为 {config.PERCENTILE_INDICATORS} 之一）")

    pool_values: dict[tuple[str, str], list[float]] = {}
    for code, pool in pools.items():
        value = values.get(code)
        if is_missing(value):
            continue
        pool_values.setdefault((pool.market, pool.group), []).append(float(value))

    result: dict[str, float | None] = {}
    for code, pool in pools.items():
        value = values.get(code)
        if is_missing(value):
            result[code] = None
            continue
        own = float(value)
        if indicator in NEGATIVE_FORCES_ZERO and own < 0:
            result[code] = 0.0  # §8 / D-19
            continue
        result[code] = percentile_score(pool_values[(pool.market, pool.group)], own)
    return result


__all__ = [
    "NEGATIVE_FORCES_ZERO",
    "PoolInfo",
    "build_pools",
    "percentile_score",
    "pool_sub_scores",
    "unclassified_codes",
]
