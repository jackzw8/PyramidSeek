"""质量分子分映射（任务 M2-2）。

《评分算法规格》§4.2 的**方式一 · 绝对值分段**：分段线性插值 + 超界截断到
``[config.SUB_SCORE_MIN, config.SUB_SCORE_MAX]``，即 ``[0, 100]``。

**边界（刻意的分工）**：§4.2 的**方式二 · 行业分位**（``net_profit_scale`` /
``asset_turnover``）需要行业对比池，按《产品开发计划》归 **M2-4**（`engine/industry.py`）。
本模块只管 5 项绝对值指标（= ``config.ABS_ANCHORS`` 的键），不越界实现分位。

**子分不做取整**：§4.2 伪代码返回的是插值原值，本节也照此返回（不 round）。
取整只发生在组装阶段（M2-5 的 ``Q``）；双引擎比对（M2-6）用容差。
子分的比对待确认事项与 :mod:`indicators` 的取整口径同属《计划完成情况》**D-18**。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .. import config
from ..common import is_missing, round_half_up
from .indicators import INDICATOR_KEYS, RawIndicators, annual_rows, raw_indicators
from .industry import PoolInfo, build_pools, pool_sub_scores

# 走绝对值锚点的指标（= config.ABS_ANCHORS 的键，顺序一致；家法 R12 唯一来源）
ABS_INDICATOR_KEYS: tuple[str, ...] = tuple(config.ABS_ANCHORS)


@dataclass(frozen=True)
class SubScores:
    """绝对值类子分（每项 0–100）。

    :attr:`missing` 记录**输入缺失**的指标（其子分按 ``0`` 给出）：§8 的「缺失值 → 用行业
    均值填充」需要行业池，属 M2-4 / M2-5；本模块只如实反映"没有输入值"，
    不让调用方把"缺数据"误当"真的得 0 分"（家法 R11）。
    """

    values: dict[str, float] = field(default_factory=dict)
    missing: tuple[str, ...] = ()

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self.values)


def interp_anchor(x: float, anchors: Sequence[tuple[float, float]]) -> float:
    """按锚点分段线性插值，区间外截断到 ``[0, 100]``（§4.2 伪代码逐字实现）。

    :param anchors: 按 ``x`` **升序**的 ``[(x_i, y_i)]``；首尾即截断边界
        （``config.ABS_ANCHORS`` 已满足：首项 ``(0, 0)``、末项为该指标的"满分阈值"）。
    :raises ValueError: 锚点为空，或 ``x`` 未升序（**不静默排序**：配置写错要立刻暴露）。

    >>> interp_anchor(0.10, [(0.0, 0.0), (0.10, 50.0), (0.20, 80.0), (0.35, 100.0)])
    50.0
    >>> interp_anchor(0.05, [(0.0, 0.0), (0.10, 50.0), (0.20, 80.0), (0.35, 100.0)])
    25.0
    >>> interp_anchor(-0.5, [(0.0, 0.0), (0.10, 50.0), (0.20, 80.0), (0.35, 100.0)])
    0.0
    """
    if not anchors:
        raise ValueError("anchors 不能为空")
    for (x0, _), (x1, _) in zip(anchors, anchors[1:], strict=False):
        if x1 < x0:
            raise ValueError(f"anchors 必须按 x 升序：{(x0, x1)!r}")

    if x <= anchors[0][0]:
        return float(config.SUB_SCORE_MIN)
    if x >= anchors[-1][0]:
        return float(config.SUB_SCORE_MAX)
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:], strict=False):
        if x0 <= x <= x1:
            if x1 == x0:  # 防御：重复锚点，避免除零（配置写错时不抛 ZeroDivisionError）
                return float(y1)
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(config.SUB_SCORE_MIN)  # 伪代码的兜底分支（升序锚点下不可达）


def absolute_sub_score(key: str, value: Any) -> float:
    """单个指标的绝对值子分（§4.2 方式一）。

    负值与零一律落进首档 → ``0`` 分（§4.2「0 分：≤0%」；§8「负值 → 规模分取 0」同向）。
    缺失（``None`` / ``NaN``）同样返回 ``0``，但调用方应改用
    :func:`absolute_sub_scores` 以同时拿到 :attr:`SubScores.missing` 留痕。
    """
    if key not in config.ABS_ANCHORS:
        raise KeyError(f"{key!r} 不是绝对值锚点指标（应为 {ABS_INDICATOR_KEYS}）")
    if is_missing(value):
        return float(config.SUB_SCORE_MIN)
    return interp_anchor(float(value), config.ABS_ANCHORS[key])


def absolute_sub_scores(
    values: dict[str, Any] | RawIndicators,
    *,
    unavailable: Sequence[str] = (),
) -> SubScores:
    """把指标映射为 5 项绝对值子分（§4.2 方式一）。

    :param values: **推荐传 M2-1 的** :class:`~..engine.indicators.RawIndicators` ——
        M2-1 对"算不出来"的指标给的值与"真的算出 0"无法从数值上区分（都是 ``0.0``），
        缺失信息只在它的 ``unavailable`` 里；传对象才会随行带过来。
        也接受裸 ``{指标: 值}`` 字典，此时若在意缺失语义，请用 :paramref:`unavailable` 显式说明。
    :param unavailable: 传裸字典时，用它声明哪些指标是"算不出来"（值虽是 0，语义是缺数据）。

    **为什么值得这么绕**（M2-1 自测抓到的真问题）：``0.0`` 既是"真的得 0 分"也是"没有数据"
    的默认值。若不把两者分开，工行（``gross_profit`` 全为 null）会看起来像"毛利率确实是 0"，
    而 §8 要求这种情况用**行业均值**填充 —— 两者处理方式完全不同（家法 R11）。
    """
    if isinstance(values, RawIndicators):
        rows: dict[str, Any] = values.values
        unavailable = values.unavailable
    else:
        rows = values
    absent = set(unavailable)

    subs: dict[str, float] = {}
    missing: list[str] = []
    for key in ABS_INDICATOR_KEYS:
        value = rows.get(key)
        if is_missing(value) or key in absent:
            missing.append(key)
        subs[key] = absolute_sub_score(key, value)
    return SubScores(values=subs, missing=tuple(missing))


# ── 成长分（M2-3，《评分算法规格》§6）─────────────────────────────────────
GROWTH_ITEM_KEYS: tuple[str, ...] = ("growth_np", "growth_ocf")
# 键名与《数据字典》§3.5 的列名对齐（growth_np / growth_ocf），便于 M2-5 组装与落库
_SERIES_FIELDS: dict[str, str] = {"growth_np": "net_profit", "growth_ocf": "op_cashflow"}
MIN_GROWTH_POINTS = 2  # 至少两点才形成一个间隔，才谈得上"复合增速"


@dataclass(frozen=True)
class GrowthScore:
    """成长分（§6）：两项单项分与合计 ``G``。"""

    items: dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    unavailable: tuple[str, ...] = ()  # 序列不足两点（或全缺失）→ 该项 0 分，并在此留痕


def _clean_series(series: Sequence[Any]) -> list[float]:
    """剔除缺失值（``None`` / ``NaN``）。

    有洞的序列不能直接当"近 5 年"用 —— 复合增速里的 ``n`` 是**间隔数**，
    混进空洞年份会把 n 算大、把增速算小。
    """
    return [float(value) for value in series if not is_missing(value)]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _pstdev(values: Sequence[float]) -> float:
    """**总体**标准差（``ddof=0``）。§6 只写 ``std(s)``，口径已澄清为总体（与原型 / JS 一致）。"""
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def cagr(series: Sequence[float]) -> float:
    """近 N 年复合增速（§6 伪代码）。

    首值为正且末值为正 → ``(y1/y0)**(1/n) - 1``；
    否则退化为 ``(y1-y0)/|y0|/n``（首值非正时开方无意义、方向也会失真）；
    首值为 0（除零）或序列不足两点 → ``0.0``。
    """
    values = [float(value) for value in series]
    n = len(values) - 1
    if n <= 0:
        return 0.0
    first, last = values[0], values[-1]
    if first > 0 and last > 0:
        return (last / first) ** (1.0 / n) - 1.0
    if first == 0:
        return 0.0
    return (last - first) / abs(first) / n


def stability_k(series: Sequence[float]) -> float:
    """稳定性系数 ``k ∈ [config.K_MIN, config.K_MAX]``（§6 伪代码）。

    ``cv = 总体标准差 ÷ |均值|``；均值恰为 0 → 直接取 ``K_MIN``（无法定义相对波动）。
    k 保留 :data:`config.STABILITY_DECIMALS` 位（§6 已补明，见 D-20）。
    """
    values = [float(value) for value in series]
    mean = _mean(values)
    if mean == 0:
        return float(config.K_MIN)
    cv = _pstdev(values) / abs(mean)
    if cv <= config.CV_LOW:
        return float(config.K_MAX)
    if cv >= config.CV_HIGH:
        return float(config.K_MIN)
    ratio = (cv - config.CV_LOW) / (config.CV_HIGH - config.CV_LOW)
    span = config.K_MAX - config.K_MIN
    # 取整走 round_half_up（与 JS 的 Math.round 一致），不用内置 round（银行家舍入）
    return round_half_up(config.K_MAX - span * ratio, config.STABILITY_DECIMALS)


def growth_item(series: Sequence[float]) -> float:
    """单项成长分 = 方向分 × 稳定性系数，截断到 ``±config.GROWTH_MAX``（§6 伪代码）。

    序列（剔除缺失后）不足两点 → ``0.0``：不是一个可比的区间。
    """
    values = _clean_series(series)
    if len(values) < MIN_GROWTH_POINTS:
        return 0.0
    direction = max(-1.0, min(1.0, cagr(values) / config.CAGR_CAP)) * config.GROWTH_MAX
    return round_half_up(direction * stability_k(values), config.GROWTH_ITEM_DECIMALS)


def growth_score(
    net_profit_series: Sequence[Any],
    op_cashflow_series: Sequence[Any],
) -> GrowthScore:
    """两项成长分与合计 ``G``（§6：``G = 净利项 + 现金流项``）。

    单项**先各自取整再相加**（``config.GROWTH_ITEM_DECIMALS``），禁止"先加总再取整"——
    后者会让双引擎在此处分歧。两项各在 ``±GROWTH_MAX`` 内 ⇒ ``|G| ≤ GROWTH_MAX_TOTAL``
    （由构造保证，不额外 clamp；§7 只在总分处 clamp）。
    """
    series_map: dict[str, Sequence[Any]] = {
        "growth_np": net_profit_series,
        "growth_ocf": op_cashflow_series,
    }
    items: dict[str, float] = {}
    unavailable: list[str] = []
    for key in GROWTH_ITEM_KEYS:
        values = _clean_series(series_map[key])
        if len(values) < MIN_GROWTH_POINTS:
            unavailable.append(key)
        items[key] = growth_item(values)
    total = round_half_up(sum(items.values()), config.GROWTH_ITEM_DECIMALS)
    return GrowthScore(items=items, total=total, unavailable=tuple(unavailable))


def growth_from_rows(
    financials: list[dict[str, Any]] | None,
    *,
    years: int = config.YEARS,
    as_of: str | None = None,
) -> GrowthScore:
    """从财报行取近 ``years`` 个年报的净利 / 经营现金流序列并算成长分（供 M2-5 组装）。

    窗口口径与 M2-1 完全一致（共用 :func:`~..engine.indicators.annual_rows`）：
    **只吃年报**、升序、最多 ``config.YEARS`` 年。
    """
    rows = annual_rows(financials, years=years, as_of=as_of)
    return growth_score(
        [row.get(_SERIES_FIELDS["growth_np"]) for row in rows],
        [row.get(_SERIES_FIELDS["growth_ocf"]) for row in rows],
    )


# ── 组装总分（M2-5，《评分算法规格》§7 / §11）──────────────────────────────
# 雷达六维的顶点顺序（契约 §3.4：顶 ROE 顺时针），与 `score_snapshot` 的列顺序一致
RADAR_KEYS: tuple[str, ...] = (
    "roe",
    "gross_margin",
    "asset_turnover",
    "eps",
    "cash_ratio",
    "net_margin",
)
# 这两维取**分位**子分，其余取绝对值子分（§4.2 + §11 口径清单）
RADAR_PERCENTILE_KEYS = frozenset({"asset_turnover", "eps"})
# §8：金融类的毛利率 / 周转率「不适用 → 单列」，不因此判「不入榜」
FINANCIAL_INAPPLICABLE = ("gross_margin", "asset_turnover")
# Q = Σ(sub×w) ÷ 100 —— 除数由 config 推导（子分满分 × 权重合计 ÷ Q 上限），不写死
_QUALITY_DIVISOR = config.SUB_SCORE_MAX * sum(config.WEIGHTS.values()) / config.Q_MAX


def quality_score(subs: dict[str, float] | SubScores) -> float:
    """质量分 Q（§2 / §11）：**先 `round(Σ(sub×w), 2)` 再 ÷ 100**。

    ⚠️ 顺序是硬要求：`round(Σ/100, 2)` 与本实现在 ``.xx5`` 边界会差 0.01，
    M2-6 双引擎逐项比对必然失败（参考实现 `engine.js` 就是先 `toFixed` 再除）。
    """
    values = subs.values if isinstance(subs, SubScores) else subs
    weighted = 0.0
    for key, weight in config.WEIGHTS.items():
        value = values.get(key)
        if not is_missing(value):
            weighted += float(value) * weight
    return round_half_up(weighted, config.Q_DECIMALS) / _QUALITY_DIVISOR


def total_score(quality: float, growth: float) -> float:
    """金字塔分（§7 / §11）：``round(clamp(Q+G, 0, 1000), 2)`` —— **先 clamp 再 round**。"""
    clamped = min(max(quality + growth, config.SCORE_MIN), config.SCORE_MAX)
    return round_half_up(clamped, config.SCORE_DECIMALS)


def radar_scores(absolute: dict[str, float], percentile: dict[str, float]) -> dict[str, float]:
    """雷达六维（§4.2 / §11）：六项都取 0~100 的**子分**；`asset_turnover` / `eps` 用分位子分。"""
    return {
        key: float(percentile.get(key, 0.0))
        if key in RADAR_PERCENTILE_KEYS
        else float(absolute.get(key, 0.0))
        for key in RADAR_KEYS
    }


@dataclass(frozen=True)
class StockScore:
    """单只股票的完整评分结果（字段名与《数据字典》§5 `score_snapshot` 对齐）。"""

    code: str
    quality: float
    growth: float
    growth_np: float
    growth_ocf: float
    score: float
    subs: dict[str, float] = field(default_factory=dict)
    radar: dict[str, float] = field(default_factory=dict)
    compare_level: str = ""
    compare_group: str = ""
    rank_in_group: int = 0
    is_top10pct: bool = False
    eligible: bool = True
    filled: tuple[str, ...] = ()  # 用行业均值填充过的指标（§8：置信度降低）
    unfilled: tuple[str, ...] = ()  # 既无值、池内也无均值 → 保持 0 并标记
    group_key: str = ""  # 内部用：分市场 + 组名（不落库）

    def snapshot_row(self, snapshot_version: str) -> dict[str, Any]:
        """→ `score_snapshot` 的一行（列名逐字对齐《数据字典》§5）。"""
        row: dict[str, Any] = {
            "code": self.code,
            "snapshot_version": snapshot_version,
            "quality": self.quality,
            "growth": self.growth,
            "growth_np": self.growth_np,
            "growth_ocf": self.growth_ocf,
            "score": self.score,
            "compare_level": self.compare_level,
            "compare_group": self.compare_group,
            "rank_in_group": self.rank_in_group,
            "is_top10pct": self.is_top10pct,
        }
        row.update({f"sub_{key}": value for key, value in self.subs.items()})
        row.update({f"radar_{key}": value for key, value in self.radar.items()})
        return row


def _pool_means(
    pools: dict[str, PoolInfo], raws: dict[str, RawIndicators]
) -> dict[tuple[str, str], dict[str, float]]:
    """每个对比池内各**可用**指标的均值（§8 填充用；池内该指标全缺 → 不出现在结果里）。"""
    collected: dict[tuple[str, str], dict[str, list[float]]] = {}
    for pool in pools.values():
        bucket = collected.setdefault((pool.market, pool.group), {})
        for member in pool.members:
            raw = raws.get(member)
            if raw is None:
                continue
            for indicator, value in raw.values.items():
                if indicator in raw.unavailable or is_missing(value):
                    continue
                bucket.setdefault(indicator, []).append(float(value))
    return {
        key: {indicator: sum(values) / len(values) for indicator, values in bucket.items()}
        for key, bucket in collected.items()
    }


def _rank_within_groups(rows: list[StockScore]) -> list[StockScore]:
    """§7：组内按 `score` 降序 → `rank_in_group`；`is_top10pct = rank ≤ ceil(n × 10%)`。

    同分按 `code` 升序，保证**确定性**（参考实现是 `sort` 后按 index 给 rank，
    同分谁在前取决于输入顺序；M2-6 对账时以本文为准）。
    """
    grouped: dict[str, list[StockScore]] = {}
    for row in rows:
        grouped.setdefault(row.group_key, []).append(row)
    ranked: list[StockScore] = []
    for members in grouped.values():
        ordered = sorted(members, key=lambda row: (-row.score, row.code))
        cut = math.ceil(len(ordered) * config.TOP_PCT)
        for index, row in enumerate(ordered, start=1):
            ranked.append(replace(row, rank_in_group=index, is_top10pct=index <= cut))
    return ranked


def score_universe(
    stocks: Sequence[dict[str, Any]],
    financials: dict[str, Sequence[dict[str, Any]]],
    *,
    as_of: str | None = None,
    years: int = config.YEARS,
) -> list[StockScore]:
    """整批组装：指标 → 绝对值子分 → 行业分位 → Q → 总分 → 组内排名（§1 的输出清单）。

    §8「缺失值 → 用行业均值填充该项」在这里落地：填充用的是**同一对比池**内该指标的均值，
    并把填充过的指标记进 :attr:`StockScore.filled`（置信度降低）；池里也没有 → 保持 0
    并记进 :attr:`StockScore.unfilled`；若它属于**加权**指标且不是金融类「不适用」项，
    则该股 :attr:`StockScore.eligible=False`（不入主榜）。
    """
    raws = {
        str(stock["code"]): raw_indicators(
            stock, list(financials.get(str(stock["code"])) or []), as_of=as_of, years=years
        )
        for stock in stocks
    }
    growths = {
        str(stock["code"]): growth_from_rows(
            list(financials.get(str(stock["code"])) or []), years=years, as_of=as_of
        )
        for stock in stocks
    }
    pools = build_pools(stocks)
    means = _pool_means(pools, raws)

    adjusted: dict[str, dict[str, float]] = {}
    filled: dict[str, tuple[str, ...]] = {}
    unfilled: dict[str, tuple[str, ...]] = {}
    for stock in stocks:
        code = str(stock["code"])
        raw = raws[code]
        pool = pools.get(code)
        pool_mean = means.get((pool.market, pool.group), {}) if pool is not None else {}
        values = dict(raw.values)
        filled_list: list[str] = []
        unfilled_list: list[str] = []
        for indicator in INDICATOR_KEYS:
            if indicator not in raw.unavailable:
                continue
            mean_value = pool_mean.get(indicator)
            if mean_value is None:
                unfilled_list.append(indicator)
            else:
                values[indicator] = mean_value
                filled_list.append(indicator)
        adjusted[code] = values
        filled[code] = tuple(filled_list)
        unfilled[code] = tuple(unfilled_list)

    percentile_subs = {
        # pool_sub_scores 要的是「每只股票该指标的**标量**」，不是整张指标表
        indicator: pool_sub_scores(
            indicator,
            {code: values.get(indicator) for code, values in adjusted.items()},
            pools,
        )
        for indicator in config.PERCENTILE_INDICATORS
    }

    rows: list[StockScore] = []
    for stock in stocks:
        code = str(stock["code"])
        pool = pools.get(code)
        absolute = absolute_sub_scores(adjusted[code], unavailable=unfilled[code])

        percentile_values: dict[str, float] = {}
        for indicator, table in percentile_subs.items():
            value = table.get(code)
            percentile_values[indicator] = 0.0 if is_missing(value) else float(value)

        # `subs` **只放加权的那 6 项**（与参考实现一致，也正好是 `score_snapshot` 的 6 个
        # `sub_` 列）：`net_margin` / `eps` 只进雷达，不进子分 —— 多写会多出 DDL 里没有的列。
        subs: dict[str, float] = {}
        for indicator in config.WEIGHTS:
            if indicator in percentile_values:
                subs[indicator] = percentile_values[indicator]
            else:
                subs[indicator] = float(absolute.values.get(indicator, 0.0))

        growth = growths[code]
        quality = quality_score(subs)
        # §8：金融类「毛利率 / 周转率不适用 → 单列」—— 单列意味着**照算照列**，
        # 不能因为这些指标天然缺失就判它「不入主榜」
        eligible = bool(stock.get("is_financial")) or not [
            key for key in config.WEIGHTS if key in unfilled[code]
        ]
        rows.append(
            StockScore(
                code=code,
                quality=quality,
                growth=growth.total,
                growth_np=growth.items.get("growth_np", 0.0),
                growth_ocf=growth.items.get("growth_ocf", 0.0),
                score=total_score(quality, growth.total),
                subs=subs,
                radar=radar_scores(dict(absolute.values), percentile_values),
                compare_level=pool.level if pool is not None else "",
                compare_group=pool.group if pool is not None else "",
                eligible=eligible,
                filled=filled[code],
                unfilled=unfilled[code],
                group_key=f"{pool.market}:{pool.group}" if pool is not None else "",
            )
        )
    return _rank_within_groups(rows)


__all__ = [
    "ABS_INDICATOR_KEYS",
    "FINANCIAL_INAPPLICABLE",
    "GROWTH_ITEM_KEYS",
    "MIN_GROWTH_POINTS",
    "RADAR_KEYS",
    "RADAR_PERCENTILE_KEYS",
    "GrowthScore",
    "StockScore",
    "SubScores",
    "absolute_sub_score",
    "absolute_sub_scores",
    "cagr",
    "growth_from_rows",
    "growth_item",
    "growth_score",
    "interp_anchor",
    "quality_score",
    "radar_scores",
    "score_universe",
    "stability_k",
    "total_score",
]
