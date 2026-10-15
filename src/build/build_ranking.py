"""排行榜产物（任务 M3-1）。

按市场分片输出 ``ranking/A.json``、``ranking/HK.json``，结构遵循《产物契约》§3.3
（contractVersion 1.2）：

- ``rows`` = **主榜**：`status == "normal"` 且已分类（有对比池）且 ``eligible``（§8
  关键指标不缺失）；按 ``score`` 降序、同分按 ``code`` 升序给 ``rank``（市场内全榜序号）。
- ``excluded`` = **单列**：其余股票，带 ``status`` 与 ``reason``（首个命中：
  退市 > 停牌 > ST > 次新股 > 行业未分类 > 关键指标缺失）；分数照算照列供参考。
- 判定规则**只在本层实现一次**（《评分算法规格》§8 的产物落地，见《计划完成情况》D-26）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config
from ..engine.indicators import RawIndicators
from ..engine.scoring import StockScore
from . import BuildInputs, derived, write_json

# 列顺序声明（《产物契约》§3.3；前端按此渲染）
RANKING_COLUMNS: tuple[str, ...] = (
    "rank",
    "code",
    "name",
    "market",
    "l1",
    "l2",
    "group",
    "level",
    "score",
    "quality",
    "growth",
    "roe",
    "gross_margin",
    "cash_ratio",
    "debt_ratio",
    "isTop10pct",
)

# 单列行的字段（《产物契约》§3.3：不给 rank / level / group / isTop10pct）
EXCLUDED_COLUMNS: tuple[str, ...] = (
    "code",
    "name",
    "market",
    "l1",
    "l2",
    "status",
    "reason",
    "score",
    "quality",
    "growth",
)

# excluded 的 reason：按优先级从高到低（首个命中即采用，契约 §3.3）
REASON_BY_STATUS: dict[str, str] = {
    "delisted": "退市",
    "suspended": "停牌",
    "ST": "ST",
    "new": "次新股",
}
REASON_UNCLASSIFIED = "行业未分类"
REASON_INELIGIBLE = "关键指标缺失"


def _indicator_value(raw: RawIndicators | None, key: str) -> float | None:
    """评分链路的原始指标（5 年均值）：不可用 → ``None``；取 4 位（契约 §3.5）。"""
    if raw is None or key in raw.unavailable:
        return None
    return derived.round_value(raw.values.get(key), config.RATIO_DECIMALS)


def _debt_ratio(inputs: BuildInputs, stock: dict[str, Any]) -> float | None:
    """负债率近五年均值 =（总资产 − 权益）÷ 总资产（PRD §5.2，展示指标）。"""
    rows = derived.annual_window(inputs.financials.get(str(stock.get("code"))))
    mean = derived.mean_of(derived.year_series("debt_ratio", rows, stock.get("shares")))
    return derived.round_value(mean, config.RATIO_DECIMALS)


def _exclusion_reason(score: StockScore, stock: dict[str, Any], *, in_pool: bool) -> str | None:
    """不入主榜的原因（首个命中）；可入榜 → ``None``。"""
    status = str(stock.get("status") or "normal")
    if status in REASON_BY_STATUS:
        return REASON_BY_STATUS[status]
    if not in_pool:
        return REASON_UNCLASSIFIED
    if not score.eligible:
        return REASON_INELIGIBLE
    return None


def _common_fields(stock: dict[str, Any], score: StockScore) -> dict[str, Any]:
    return {
        "code": score.code,
        "name": str(stock.get("name") or score.code),
        "market": str(stock.get("market") or ""),
        "l1": str(stock.get("l1_name") or ""),
        "l2": str(stock.get("l2_name") or ""),
        "score": derived.round_value(score.score, config.SCORE_DECIMALS),
        "quality": derived.round_value(score.quality, config.Q_DECIMALS),
        "growth": derived.round_value(score.growth, config.GROWTH_ITEM_DECIMALS),
    }


def _main_row(inputs: BuildInputs, stock: dict[str, Any], score: StockScore) -> dict[str, Any]:
    """主榜一行（``rank`` 先占位，排序后回填；**键顺序即 `columns` 顺序**）。"""
    raw = inputs.raws.get(score.code)
    row: dict[str, Any] = dict.fromkeys(RANKING_COLUMNS)
    row.update({"rank": 0})
    row.update(_common_fields(stock, score))
    row.update(
        {
            "group": score.compare_group,
            "level": score.compare_level,
            "roe": _indicator_value(raw, "roe"),
            "gross_margin": _indicator_value(raw, "gross_margin"),
            "cash_ratio": _indicator_value(raw, "cash_ratio"),
            "debt_ratio": _debt_ratio(inputs, stock),
            "isTop10pct": bool(score.is_top10pct),
        }
    )
    return row


def _excluded_row(stock: dict[str, Any], score: StockScore, reason: str) -> dict[str, Any]:
    """单列一行（键顺序 = :data:`EXCLUDED_COLUMNS`）。"""
    row: dict[str, Any] = dict.fromkeys(EXCLUDED_COLUMNS)
    row.update(_common_fields(stock, score))
    row.update({"status": str(stock.get("status") or "normal"), "reason": reason})
    return row


def _sorted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``score`` 降序、同分 ``code`` 升序（保可复现，D-25）。"""
    return sorted(rows, key=lambda row: (-float(row["score"] or 0.0), str(row["code"])))


def build_ranking(inputs: BuildInputs, market: str, out_dir: Path) -> Path:
    """生成单市场排行榜产物（主榜 + 单列），返回写出路径。"""
    stocks_by_code = inputs.stocks_by_code
    main: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for score in inputs.scored:
        stock = stocks_by_code.get(score.code)
        if stock is None or str(stock.get("market") or "") != market:
            continue
        reason = _exclusion_reason(score, stock, in_pool=score.code in inputs.pools)
        if reason is None:
            main.append(_main_row(inputs, stock, score))
        else:
            excluded.append(_excluded_row(stock, score, reason))

    rows = _sorted(main)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank

    payload = {
        "meta": {
            "snapshotVersion": inputs.snapshot_version,
            "contractVersion": config.CONTRACT_VERSION,
        },
        "market": market,
        "columns": list(RANKING_COLUMNS),
        "rows": rows,
        "excluded": _sorted(excluded),
    }
    return write_json(out_dir / "ranking" / f"{market}.json", payload)


__all__ = ["EXCLUDED_COLUMNS", "RANKING_COLUMNS", "build_ranking"]
