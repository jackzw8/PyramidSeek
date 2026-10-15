"""产物生成层：ranking / stock / meta / industries + schema 校验（任务 M3-1~M3-5）。

:class:`BuildInputs` 是产物层的**唯一入口**：由 ``src/batch.py`` 从「采集/清洗（M1）+
评分（M2）」的结果组装，``build_*`` 与 ``validate`` 只读它 —— 避免各模块自行去摸
``CleanResult`` / DuckDB 造成口径分叉（家法 R13 精神）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..engine.indicators import RawIndicators
from ..engine.industry import PoolInfo
from ..engine.scoring import StockScore


@dataclass(frozen=True)
class BuildInputs:
    """产物构建的全部输入（字段只增不减）。"""

    snapshot_version: str
    generated_at: str  # UTC ISO8601（--generated-at 可固定，保幂等 R19）
    stocks: tuple[dict[str, Any], ...] = ()  # 标准层主数据（含 status / l1_name / shares / ...）
    financials: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    prices: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    scored: tuple[StockScore, ...] = ()
    raws: dict[str, RawIndicators] = field(default_factory=dict)
    pools: dict[str, PoolInfo] = field(default_factory=dict)
    degraded: bool = False
    notes: tuple[str, ...] = ()  # 降级留痕（写进 meta.notes）
    data_date_financial: str | None = None
    data_date_price: str | None = None

    @property
    def stocks_by_code(self) -> dict[str, dict[str, Any]]:
        """code → 标准层主数据行。"""
        return {str(stock.get("code")): stock for stock in self.stocks}

    @property
    def scores_by_code(self) -> dict[str, StockScore]:
        """code → 评分结果。"""
        return {row.code: row for row in self.scored}


def write_json(path: Path, payload: Any) -> Path:
    """写出产物 JSON（UTF-8、缩进 2、保留中文、结尾换行）—— 产物统一写出方式。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


__all__ = ["BuildInputs", "write_json"]
