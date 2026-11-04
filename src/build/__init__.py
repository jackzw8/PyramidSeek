"""产物生成层：ranking / stock / meta / industries + schema 校验（任务 M3-1~M3-5）。

:class:`BuildInputs` 是产物层的**唯一入口**：由 ``src/batch.py`` 从「采集/清洗（M1）+
评分（M2）」的结果组装，``build_*`` 与 ``validate`` 只读它 —— 避免各模块自行去摸
``CleanResult`` / DuckDB 造成口径分叉（家法 R13 精神）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import cached_property
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
    # M5-1：本轮实际选源（写进 meta.sourcePolicy，ADR-015）
    source_policy: dict[str, Any] = field(default_factory=dict)
    data_date_financial: str | None = None
    data_date_price: str | None = None

    # ``cached_property`` 而非 ``property``：``build_stock`` 每只股票要访问两次，
    # 全市场 5000 只 × 2 个索引 × 全量 dict 重建 = O(n²)（PL-06）。frozen dataclass
    # 不拦 ``__dict__`` 直写，缓存安全；``stocks``/``scored`` 是 tuple，组装后不再变。
    @cached_property
    def stocks_by_code(self) -> dict[str, dict[str, Any]]:
        """code → 标准层主数据行。"""
        return {str(stock.get("code")): stock for stock in self.stocks}

    @cached_property
    def scores_by_code(self) -> dict[str, StockScore]:
        """code → 评分结果。"""
        return {row.code: row for row in self.scored}


def write_json(path: Path, payload: Any) -> Path:
    """写出产物 JSON（UTF-8、缩进 2、保留中文、结尾换行）—— 产物统一写出方式。

    两条硬要求（PL-19）：

    1. **原子**：先写同目录 ``.tmp`` 再 ``os.replace`` —— 中途异常既不会留下半截产物，
       也不会把已有产物删成空缺；
    2. **拒绝 NaN / Infinity**：``allow_nan=False``。此前默认放行，``NaN`` 写成裸
       ``NaN`` 字面量 —— Python 侧 ``json.load`` 能读、``validate`` 能过，但浏览器
       ``JSON.parse`` 直接抛错，坏产物会一路进 ``data`` 分支。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


__all__ = ["BuildInputs", "write_json"]
