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

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .. import config
from ..common import is_missing
from .indicators import RawIndicators

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


__all__ = [
    "ABS_INDICATOR_KEYS",
    "SubScores",
    "absolute_sub_score",
    "absolute_sub_scores",
    "interp_anchor",
]
