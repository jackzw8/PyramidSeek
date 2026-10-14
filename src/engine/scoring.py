"""评分引擎（占位，任务 M2-2 / M2-3 / M2-5）。

金字塔分 = clamp(Q + G, 0, 1000)（《评分算法规格》§2、§4、§6、§7）。

对账要求（家法 R13 / 计划 M2-6）：Python 与 JS 双引擎共用同一 fixtures
与黄金期望输出，逐项比对 Q / G / 6 项子分 / 分位分 / 雷达六维；不一致即 CI 失败。
逐字对齐细节：单项成长分**先** ``round(..., 2)`` 再相加、``stability_k`` 中间结果
``round(..., 4)``、先 clip 后 round、除零保护。
"""

from __future__ import annotations

from typing import Any


def sub_score_abs(value: float, anchors: list[tuple[float, float]]) -> float:
    """绝对值锚点分段线性子分（未实现）。"""
    raise NotImplementedError("sub_score_abs 待 M2-2 实现")


def growth_item(series: list[float]) -> float:
    """单项成长分 = round(方向分 × 稳定性系数, 2)（未实现）。"""
    raise NotImplementedError("growth_item 待 M2-3 实现")


def score_universe(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """全样本打分并排序（未实现）。"""
    raise NotImplementedError("score_universe 待 M2-5 实现")


__all__ = ["growth_item", "score_universe", "sub_score_abs"]
