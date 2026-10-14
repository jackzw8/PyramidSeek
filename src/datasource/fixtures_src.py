"""fixtures 数据源（任务 M1-6）：三级回退的**最后一级**。

定位（《数据源接入规格》§1、《产品开发计划》M1-6、家法 R16「离线永远可跑」）：
主源（AkShare）→ 官方源（巨潮 / 披露易）→ **fixtures**。前两级都要联网，
这一级永远可用，保证链路在断网 / 数据源改版 / CI 环境里都能跑通。

与 :mod:`..batch` 的区别：``batch.load_fixtures`` 是**产物层**直接读 fixtures 当输入
（M0/M3 的占位链路）；本模块是**数据源层的适配器**，把同一份 fixtures 包装成
``DataSource`` 协议，供回退链统一调用。两者读同一份文件是刻意的——fixtures 就是
"录好的数据源响应"，不是产物。

fixtures 不含行情与评分，故 ``price_daily`` 明确抛错（让回退链继续往下 / 由上层决定）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import config
from .base import DataSourceError

# fixtures 里与"股票主数据"无关的嵌套键（避免污染标准层）
_NESTED_KEYS = ("financials", "scores", "notes")


class FixturesSource:
    """把 ``fixtures/financials.json`` 包装成标准层数据源。"""

    name = "fixture"

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else config.FIXTURES_FILE
        self._data: dict[str, Any] | None = None

    # ── 载入 ──────────────────────────────────────────────────────────────
    def _load(self) -> dict[str, Any]:
        if self._data is None:
            if not self._path.exists():
                raise DataSourceError(
                    f"fixtures 不存在：{self._path}（无法离线回退）", source=self.name
                )
            try:
                with self._path.open(encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise DataSourceError(
                    f"fixtures 读取失败：{self._path}（{exc}）", source=self.name
                ) from exc
        return self._data

    def _stocks(self) -> list[dict[str, Any]]:
        return list(self._load().get("stocks") or [])

    # ── DataSource 接口 ───────────────────────────────────────────────────
    def list_stocks(self, market: str) -> list[dict[str, Any]]:
        if market not in config.MARKETS:
            raise ValueError(f"不支持的市场：{market!r}（应为 {config.MARKETS} 之一）")
        rows = [
            {key: value for key, value in row.items() if key not in _NESTED_KEYS}
            for row in self._stocks()
            if row.get("market") == market
        ]
        if not rows:
            raise DataSourceError(f"fixtures 中无 {market} 市场样本", source=self.name)
        return rows

    def financials(self, code: str) -> list[dict[str, Any]]:
        for row in self._stocks():
            if row.get("code") != code:
                continue
            periods = row.get("financials") or {}
            if not periods:
                raise DataSourceError(f"fixtures 中 {code} 无财报数据", source=self.name, code=code)
            rows = []
            for period, item in periods.items():
                record = dict(item)
                record.setdefault("code", code)
                record.setdefault("report_period", period)
                record.setdefault("source", self.name)
                rows.append(record)
            return sorted(rows, key=lambda item: str(item.get("report_period")))
        raise DataSourceError(f"fixtures 中无此标的：{code}", source=self.name, code=code)

    def price_daily(self, code: str, adjust: str = config.DEFAULT_ADJUST) -> list[dict[str, Any]]:
        raise DataSourceError(
            f"fixtures 不含行情数据（{code}）：行情回退不在 M1 范围（月K/MACD 由 M2 起用前复权价）",
            source=self.name,
            code=code,
        )

    def industry_tree(self, market: str) -> list[dict[str, Any]]:
        """由股票主数据的行业字段反推行业树（fixtures 无独立行业文件）。"""
        stocks = self.list_stocks(market)
        standard = config.INDUSTRY_STANDARD[market]
        nodes: dict[tuple[int, str], dict[str, Any]] = {}
        for row in stocks:
            for level, code_key, name_key, parent_key in (
                (1, "l1_code", "l1_name", None),
                (2, "l2_code", "l2_name", "l1_code"),
            ):
                node_code = row.get(code_key)
                node_name = row.get(name_key)
                if not node_code or not node_name:
                    continue
                nodes[(level, node_code)] = {
                    "standard": standard,
                    "version": "fixture",
                    "level": level,
                    "node_code": node_code,
                    "node_name": node_name,
                    "parent_code": row.get(parent_key) if parent_key else None,
                    "member_count": sum(1 for other in stocks if other.get(code_key) == node_code),
                }
        if not nodes:
            raise DataSourceError(f"fixtures 中无 {market} 行业信息", source=self.name)
        return sorted(nodes.values(), key=lambda item: (item["level"], item["node_code"]))

    def health_check(self) -> bool:
        try:
            self._load()
        except DataSourceError:
            return False
        return True


__all__ = ["FixturesSource"]
