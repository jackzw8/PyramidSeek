"""产物派生展示指标（任务 M3-2；依据《产物契约》§3.4 / §3.5）。

**只服务展示**：详情页四张财务表（逐年）、排行页的 `roe` / `gross_margin` /
`cash_ratio` / `debt_ratio` 列、月K、同业 ROE 列表。评分链路（``engine/``）不消费本模块，
但**逐年比值口径与它完全一致** —— 复用 :func:`engine.indicators.year_ratio`：

- 分子 / 分母任一缺失 → 该年 ``None``（前端显示「—」）；
- 除零 → ``0``（0 是有效值，家法 R11）；
- 均值 = 有效年算术平均（跳过 ``None`` 的年份，与 §8 缺失语义一致）。

取整**不在本模块内联**：本模块返回原始值，产物构建时用 :func:`round_value` /
:func:`round_series` 按《产物契约》§3.5 取整（金额 2 位 / 比率 4 位 / EPS 2 位）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .. import config
from ..common import is_missing, round_half_up
from ..engine.indicators import RawIndicators, annual_rows, year_ratio

_SHARES = "__shares__"  # 分母来自股票主数据（亿股），不在财报行里（同 indicators）
_DERIVED_ASSETS = "__total_assets_minus_equity__"  # 负债率的分子：总资产 − 权益（PRD §5.2）

# 逐字段公式：字段 → (分子字段, 分母字段)；分母 ``None`` = 金额类（分子即值，单位亿元）。
FORMULAS: dict[str, tuple[str, str | None]] = {
    # profitability
    "roe": ("net_profit", "equity"),
    "roa": ("net_profit", "total_assets"),
    "gross_margin": ("gross_profit", "revenue"),
    "net_margin": ("net_profit", "revenue"),
    "eps": ("net_profit", _SHARES),
    # structure
    "cash_ratio": ("cash", "total_assets"),
    "debt_ratio": (_DERIVED_ASSETS, "total_assets"),
    # operation
    "asset_turnover": ("revenue", "total_assets"),
    # cashflow（金额类：亿元）
    "cash": ("cash", None),
    "net_profit": ("net_profit", None),
    "ocf": ("op_cashflow", None),
    "icf": ("icf", None),
    "fcf": ("fcf", None),
}

# 标准层暂无「应收账款」「固定资产」字段 → 这三项本期恒 ``None``（《产物契约》§3.4 已知限制）
UNAVAILABLE_FIELDS: tuple[str, ...] = ("ar_turnover", "ar_days", "fa_turnover")

# 四张财务表：表名 → 字段顺序（即产物字段顺序，与《产物契约》§3.4 一致）
TABLES: dict[str, tuple[str, ...]] = {
    "profitability": ("roe", "roa", "gross_margin", "net_margin", "eps"),
    "structure": ("cash_ratio", "debt_ratio"),
    "operation": (*UNAVAILABLE_FIELDS, "asset_turnover"),
    "cashflow": ("cash", "net_profit", "ocf", "icf", "fcf", "ncf"),
}

# 各字段取整位数（《产物契约》§3.5）
DECIMALS: dict[str, int] = {
    "roe": config.RATIO_DECIMALS,
    "roa": config.RATIO_DECIMALS,
    "gross_margin": config.RATIO_DECIMALS,
    "net_margin": config.RATIO_DECIMALS,
    "eps": config.EPS_DECIMALS,
    "cash_ratio": config.RATIO_DECIMALS,
    "debt_ratio": config.RATIO_DECIMALS,
    "asset_turnover": config.RATIO_DECIMALS,
    "ar_turnover": config.RATIO_DECIMALS,
    "ar_days": config.RATIO_DECIMALS,
    "fa_turnover": config.RATIO_DECIMALS,
    "cash": config.AMOUNT_DECIMALS,
    "net_profit": config.AMOUNT_DECIMALS,
    "ocf": config.AMOUNT_DECIMALS,
    "icf": config.AMOUNT_DECIMALS,
    "fcf": config.AMOUNT_DECIMALS,
    "ncf": config.AMOUNT_DECIMALS,
}


# ── 取整（契约 §3.5：统一走 round_half_up，禁内置 round）──────────────────────
def round_value(value: float | None, decimals: int) -> float | None:
    """单个值取整；``None`` 原样透传（缺失不是 0，家法 R11）。"""
    return None if value is None else round_half_up(float(value), decimals)


def round_series(series: Sequence[float | None], decimals: int) -> list[float | None]:
    """整条序列取整（``None`` 位保持 ``None``）。"""
    return [round_value(value, decimals) for value in series]


# ── 逐年序列 ─────────────────────────────────────────────────────────────
def _numerator(field: str, row: Mapping[str, Any], shares: Any) -> Any:
    if field == _DERIVED_ASSETS:
        assets, equity = row.get("total_assets"), row.get("equity")
        if is_missing(assets) or is_missing(equity):
            return None
        return float(assets) - float(equity)
    if field == _SHARES:
        return shares
    return row.get(field)


def year_series(key: str, rows: Sequence[Mapping[str, Any]], shares: Any) -> list[float | None]:
    """某展示字段逐年的值（与 ``rows`` 等长、升序；该年不可用 → ``None``）。

    ``ncf = ocf + icf + fcf``（三者任一为 ``None`` 则该年 ``None``，契约 §3.4）。
    """
    if key == "ncf":
        ocf = year_series("ocf", rows, shares)
        icf = year_series("icf", rows, shares)
        fcf = year_series("fcf", rows, shares)
        return [
            None if None in (a, b, c) else a + b + c  # type: ignore[operator]
            for a, b, c in zip(ocf, icf, fcf, strict=True)
        ]
    if key in UNAVAILABLE_FIELDS:
        return [None] * len(rows)
    numerator_field, denominator_field = FORMULAS[key]
    series: list[float | None] = []
    for row in rows:
        denominator = (
            None
            if denominator_field is None
            else (shares if denominator_field == _SHARES else row.get(denominator_field))
        )
        series.append(
            year_ratio(
                _numerator(numerator_field, row, shares),
                denominator,
                has_denominator=denominator_field is not None,
            )
        )
    return series


def annual_window(
    financials: Sequence[Mapping[str, Any]] | None, *, years: int = config.YEARS
) -> list[Mapping[str, Any]]:
    """近 ``years`` 个完整财年（升序）—— 包装 ``annual_rows``，产物层统一从这里取窗口。"""
    return list(annual_rows(financials, years=years))


def year_labels(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """财年标签列表（如 ``["2020", …, "2024"]``，即 ``years`` 字段）。"""
    return [str(row.get("report_period") or "")[:4] for row in rows]


def mean_of(series: Sequence[float | None]) -> float | None:
    """有效年算术平均；全年缺失 → ``None``。"""
    values = [float(value) for value in series if value is not None]
    return sum(values) / len(values) if values else None


# ── 四张财务表 ───────────────────────────────────────────────────────────
def table_series(
    rows: Sequence[Mapping[str, Any]], shares: Any
) -> dict[str, dict[str, list[float | None]]]:
    """四张财务表的**原始**逐年序列（未取整，键顺序 = 契约字段顺序）。"""
    return {
        table: {key: year_series(key, rows, shares) for key in keys}
        for table, keys in TABLES.items()
    }


# ── 月K（前复权）─────────────────────────────────────────────────────────
def _extreme(current: float | None, value: float | None, *, biggest: bool) -> float | None:
    if value is None:
        return current
    if current is None:
        return value
    return max(current, value) if biggest else min(current, value)


def _adjusted(price: Any, factor: float) -> float | None:
    return None if is_missing(price) else float(price) * factor


def monthly_kline(price_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """日行情 → 月K（**前复权**为主序列，另附 ``hfq`` 后复权子对象；契约 §3.4）。

    - 主序列 ``{months, o, h, l, c}`` = 前复权：``o`` = 当月首个交易日、``h`` / ``l`` = 当月
      极值、``c`` = 当月最后一个交易日（``close_qfq``）；o/h/l 按 ``adj_factor``
      （``= close_qfq / close``，M1-4 反算）换算；
    - ``hfq`` = 后复权 ``{o,h,l,c}`` = 未取整的前复权序列 **÷ 序列首日 ``adj_factor``**
      （期初归一到原始价；先除后取整，避免二次舍入）；
    - 无任何有效行 → ``None``（产物省略 ``kline`` 键）。
    """
    buckets: dict[str, dict[str, float | None]] = {}
    base_factor: float | None = None
    for row in sorted(price_rows, key=lambda item: str(item.get("trade_date") or "")):
        trade_date = str(row.get("trade_date") or "")[:10]
        close_qfq = row.get("close_qfq")
        if not trade_date or is_missing(close_qfq):
            continue
        factor_value = row.get("adj_factor")
        factor = 1.0 if is_missing(factor_value) else float(factor_value)
        if base_factor is None:
            base_factor = factor
        month = trade_date[:7]
        bucket = buckets.get(month)
        if bucket is None:
            bucket = {"o": _adjusted(row.get("open"), factor), "h": None, "l": None, "c": None}
            buckets[month] = bucket
        bucket["h"] = _extreme(bucket["h"], _adjusted(row.get("high"), factor), biggest=True)
        bucket["l"] = _extreme(bucket["l"], _adjusted(row.get("low"), factor), biggest=False)
        bucket["c"] = float(close_qfq)
    if not buckets:
        return None

    months = sorted(buckets)
    raw_series = {key: [buckets[month][key] for month in months] for key in ("o", "h", "l", "c")}
    result: dict[str, Any] = {"months": months}
    for key, series in raw_series.items():
        result[key] = [round_value(value, config.PRICE_DECIMALS) for value in series]

    # 后复权 = 前复权 ÷ 序列首日 ``adj_factor``；**仅当首日因子为正**才成立 ——
    # 长期高分红的标的，其前复权价在早期可能为**负**（新浪口径：长江电力 2003 年 -8.33 元），
    # 此时比率无意义、照算会反号 → 不输出 ``hfq``（契约 §3.4 里 hfq 是可选子对象，
    # 前端据「有无 hfq」禁用复权切换）。
    base = base_factor if base_factor else 1.0
    if base > 0:
        result["hfq"] = {
            key: [
                round_value(None if value is None else value / base, config.PRICE_DECIMALS)
                for value in series
            ]
            for key, series in raw_series.items()
        }
    return result


# ── 同业 ROE 列表 ────────────────────────────────────────────────────────
def peer_rows(
    members: Iterable[str],
    stocks_by_code: Mapping[str, Mapping[str, Any]],
    raws: Mapping[str, RawIndicators],
    *,
    decimals: int = config.RATIO_DECIMALS,
) -> list[dict[str, Any]]:
    """对比池成员（**含本股**）的 ``{code, name, roe5y}``。

    排序：``roe5y`` 降序、缺失垫底、同值按 ``code`` 升序。
    """
    rows: list[dict[str, Any]] = []
    for code in members:
        raw = raws.get(code)
        roe = None
        if raw is not None and "roe" not in raw.unavailable:
            roe = round_value(raw.values.get("roe"), decimals)
        rows.append(
            {
                "code": code,
                "name": str((stocks_by_code.get(code) or {}).get("name") or code),
                "roe5y": roe,
            }
        )
    rows.sort(key=lambda item: (item["roe5y"] is None, -(item["roe5y"] or 0.0), item["code"]))
    return rows


__all__ = [
    "DECIMALS",
    "FORMULAS",
    "TABLES",
    "UNAVAILABLE_FIELDS",
    "annual_window",
    "mean_of",
    "monthly_kline",
    "peer_rows",
    "round_series",
    "round_value",
    "table_series",
    "year_labels",
    "year_series",
]
