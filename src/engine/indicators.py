"""指标计算（任务 M2-1）。

《评分算法规格》§3 的 8 项原始指标。**唯一实现依据是该文档**；改公式须升
`formulaVersion` 并同步 06 文档（家法 R2）。

口径（§3 原文 + §8 边界）：

- 一律"**逐年算比值，再取近 5 个完整财年的算术平均**"（不是 5 年合计相除）；
- **除零返回 0**（该年贡献 ``0.0``，参与均值）；
- **缺失（``None``）不参与均值**：某年分子或分母缺失 → 该年跳过；某指标全部年份都算不出来
  → 值取 ``0.0`` 并记入 :attr:`RawIndicators.unavailable`。§8 的「缺失值用行业均值填充」
  需要行业池，属 M2-4 / M2-5 的动作 —— M2-1 只如实上报"这项算不出来"，
  不假装算出来了（家法 R11：缺失不得用 0 冒名顶替而不留痕）；
- **只吃年报**：中报 / 季报是累计口径（``is_cumulative``），不可比，按 §3 不参与主评分；
- 金融股（``is_financial``）的 ``gross_margin`` / ``asset_turnover`` 会因 ``gross_profit``
  缺失而落进 ``unavailable`` —— 这正是 §8「金融类毛利率 / 周转率不适用 → 单列」的
  数据表现，无需特判。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import config
from ..common import is_missing

# 8 项指标（顺序与《评分算法规格》§3 表格一致；双引擎对账按此顺序逐项比对）
INDICATOR_KEYS: tuple[str, ...] = (
    "roe",
    "roa",
    "gross_margin",
    "net_margin",
    "cash_ratio",
    "asset_turnover",
    "net_profit_scale",
    "eps",
)
# 比率类（无量纲）
RATIO_INDICATORS: tuple[str, ...] = (
    "roe",
    "roa",
    "gross_margin",
    "net_margin",
    "cash_ratio",
    "asset_turnover",
)
# 规模类（单位：亿元）
SCALE_INDICATORS: tuple[str, ...] = ("net_profit_scale",)
# 量纲分类仅用于**展示与落库取整**（如产物里保留几位）；**计算链路一律不取整**（D-18）：
# 中间取整会让"接近但不等"的值被抹成并列，直接改变分位分（engine.js 也不取整）。

_SHARES = "__shares__"  # 分母来自股票主数据（亿股），不在财报行里

# 公式表（§3）：指标 → (分子字段, 分母字段)。分母 ``None`` = 直接对分子取均值。
# 单位自洽：金额一律亿元、股本一律亿股 → eps 的数值即「元/股」。
_FORMULAS: dict[str, tuple[str, str | None]] = {
    "roe": ("net_profit", "equity"),
    "roa": ("net_profit", "total_assets"),
    "gross_margin": ("gross_profit", "revenue"),
    "net_margin": ("net_profit", "revenue"),
    "cash_ratio": ("cash", "total_assets"),
    "asset_turnover": ("revenue", "total_assets"),
    "net_profit_scale": ("net_profit", None),
    "eps": ("net_profit", _SHARES),
}


@dataclass(frozen=True)
class RawIndicators:
    """单只股票的 8 项原始指标（M2-2 起据此算子分与雷达）。"""

    code: str = ""
    values: dict[str, float] = field(default_factory=dict)
    periods: tuple[str, ...] = ()  # 实际参与计算的报告期（升序，最多 config.YEARS 个）
    unavailable: tuple[str, ...] = ()  # 无任何可用年份的指标（值按 0.0 返回，但此处留痕）

    def get(self, key: str) -> float:
        return self.values.get(key, 0.0)


def _is_annual(row: dict[str, Any]) -> bool:
    """是否年报：优先信 ``report_type``，缺字段时退回看报告期是否为 12-31。"""
    report_type = row.get("report_type")
    if report_type:
        return str(report_type) == "annual"
    return str(row.get("report_period") or "").endswith("-12-31")


def annual_rows(
    financials: list[dict[str, Any]] | None,
    *,
    years: int = config.YEARS,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    """取近 ``years`` 个完整财年（**升序**）。

    :param as_of: 截止日（``YYYY-MM-DD``）；给了就只保留 ``effective_date`` 已到的期次
        （家法 R9：报告期 + 4 个月才生效）。``None``（默认）不按生效日过滤 ——
        这样默认行为与 §3「近 5 个财年」一致，需要"只用已生效数据"时由调用方显式传。
    """
    picked: list[dict[str, Any]] = []
    for row in financials or []:
        if not _is_annual(row):
            continue
        if as_of is not None:
            effective = row.get("effective_date")
            if effective is not None and str(effective) > as_of:
                continue
        picked.append(row)
    picked.sort(key=lambda item: str(item.get("report_period") or ""))
    return picked[-max(years, 0) :] if years > 0 else []


def _year_ratio(numerator: Any, denominator: Any, *, has_denominator: bool = True) -> float | None:
    """单年比值：缺失 → ``None``（该年跳过）；**除零 → 0.0**（§3）。

    ``has_denominator=False`` 表示该指标本来就没有分母（如 ``net_profit_scale``）。

    **注意**：必须把"没有分母"与"分母缺失"分开 —— 二者都会让 ``denominator`` 是 ``None``，
    但语义完全不同（前者取分子本身，后者该年不可用）。混为一谈会让 ``shares`` 缺失的股票
    把 eps 算成"净利润本身"（M2-1 自测抓到过这个错）。
    """
    if is_missing(numerator):
        return None
    if not has_denominator:
        return float(numerator)
    if is_missing(denominator):
        return None
    bottom = float(denominator)
    if bottom == 0:
        return 0.0
    return float(numerator) / bottom


def raw_indicators(
    stock: dict[str, Any],
    financials: list[dict[str, Any]] | None = None,
    *,
    years: int = config.YEARS,
    as_of: str | None = None,
) -> RawIndicators:
    """计算一只股票的 8 项原始指标（《评分算法规格》§3）。

    :param stock: 股票主数据行（取 ``code``、``shares``；``shares`` 单位亿股）。
    :param financials: 该股票的财报行（标准层字段，金额单位亿元）。
    :returns: :class:`RawIndicators` —— 8 项值与"哪些项算不出来"的留痕。

    >>> raw = raw_indicators({"code": "SH600900", "shares": 10.0}, [
    ...     {"report_period": "2024-12-31", "report_type": "annual",
    ...      "net_profit": 10.0, "equity": 100.0, "revenue": 100.0,
    ...      "total_assets": 200.0, "gross_profit": 40.0, "cash": 20.0},
    ... ])
    >>> raw.values["roe"]
    0.1
    >>> raw.values["eps"]
    1.0
    """
    rows = annual_rows(financials, years=years, as_of=as_of)
    shares = stock.get("shares")
    values: dict[str, float] = {}
    unavailable: list[str] = []

    for key in INDICATOR_KEYS:
        numerator_field, denominator_field = _FORMULAS[key]
        samples: list[float] = []
        for row in rows:
            denominator: Any
            if denominator_field is None:
                denominator = None
            elif denominator_field == _SHARES:
                denominator = shares
            else:
                denominator = row.get(denominator_field)
            ratio = _year_ratio(
                row.get(numerator_field),
                denominator,
                has_denominator=denominator_field is not None,
            )
            if ratio is not None:
                samples.append(ratio)
        if samples:
            # 计算链路**不取整**（D-18）：中间取整会把"接近但不等"的值抹成并列，
            # 直接改变分位分；engine.js 同样不取整。取整只发生在「与 JS 对齐的点」
            # 与「落库/产物字段」两处边界。
            values[key] = sum(samples) / len(samples)
        else:
            values[key] = 0.0
            unavailable.append(key)

    return RawIndicators(
        code=str(stock.get("code") or ""),
        values=values,
        periods=tuple(str(row.get("report_period") or "") for row in rows),
        unavailable=tuple(unavailable),
    )


__all__ = [
    "INDICATOR_KEYS",
    "RATIO_INDICATORS",
    "SCALE_INDICATORS",
    "RawIndicators",
    "annual_rows",
    "raw_indicators",
]
