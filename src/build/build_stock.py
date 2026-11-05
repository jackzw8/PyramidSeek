"""个股详情产物（任务 M3-2）。

一码一文件 ``stock/{code}.json``，结构遵循《产物契约》§3.4：

- 评分 / 子分 / 雷达六维（雷达键顺序 = 顶点顺序，自顶部顺时针）；
- 四张财务表：与 ``years`` 等长的逐年数组，缺失年 ``null``（§3.5；`ar_turnover` /
  `ar_days` / `fa_turnover` 因标准层无对应字段恒 ``null``）；
- ``kline`` 可选：有行情才出（**前复权**月K），无行情省略该键；
- ``peers``：对比池全部成员（含本股），按 ``roe5y`` 降序。

**排除股票（ST / 次新 / 未分类…）同样生成** —— 它们不进主榜但仍可被详情页 / 观察池访问。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config
from ..engine.scoring import RADAR_KEYS
from . import BuildInputs, derived, write_json


def _subs(score_subs: dict[str, float]) -> dict[str, Any]:
    """6 项加权子分（顺序 = 权重表；2 位，与 `score_snapshot` 的 DECIMAL(6,2) 同口径）。"""
    return {
        key: derived.round_value(score_subs.get(key, 0.0), config.Q_DECIMALS)
        for key in config.WEIGHTS
    }


def _radar(radar: dict[str, float]) -> dict[str, Any]:
    """雷达六维（键顺序 = 顶点顺序，契约 §3.4；2 位）。"""
    return {key: derived.round_value(radar.get(key, 0.0), config.Q_DECIMALS) for key in RADAR_KEYS}


def _tables(rows: list[dict[str, Any]]) -> dict[str, dict[str, list[Any]]]:
    """四张财务表：逐年数组按字段位数取整（契约 §3.5）。"""
    raw_tables = derived.table_series(rows)
    return {
        table: {
            key: derived.round_series(series, derived.DECIMALS[key])
            for key, series in columns.items()
        }
        for table, columns in raw_tables.items()
    }


def build_stock(inputs: BuildInputs, code: str, out_dir: Path) -> Path:
    """生成个股详情产物，返回写出路径。"""
    stock = inputs.stocks_by_code[code]
    score = inputs.scores_by_code[code]
    rows = derived.annual_window(inputs.financials.get(code))

    payload: dict[str, Any] = {
        "code": code,
        "name": str(stock.get("name") or code),
        "market": str(stock.get("market") or ""),
        "exchange": str(stock.get("exchange") or ""),
        "l1": str(stock.get("l1_name") or ""),
        "l2": str(stock.get("l2_name") or ""),
        "group": score.compare_group,
        "level": score.compare_level,
        "industryStd": str(stock.get("industry_std") or ""),
        "listedAt": str(stock.get("listed_at") or ""),
        "score": derived.round_value(score.score, config.SCORE_DECIMALS),
        "quality": derived.round_value(score.quality, config.Q_DECIMALS),
        "growth": derived.round_value(score.growth, config.GROWTH_ITEM_DECIMALS),
        "growthDetail": {
            "netProfit": derived.round_value(score.growth_np, config.GROWTH_ITEM_DECIMALS),
            "opCashflow": derived.round_value(score.growth_ocf, config.GROWTH_ITEM_DECIMALS),
        },
        "qualitySubs": _subs(score.subs),
        # 指标可信度（D-65）：`filled` = 用同行均值顶替的项，`unfilled` = 连池均值都没有的项。
        # 原先这两个标记只活在内存里，产物不输出 → 用户看到「毛利率空着、分数却有一项」，
        # 无从核对。前端可据此标注“含 N 项估算”。
        "filled": list(score.filled),
        "unfilled": list(score.unfilled),
        "radar": _radar(score.radar),
        "years": derived.year_labels(rows),
        **_tables(rows),
    }

    # `kline` 字段**已退役**（D-56 / M4-7，2026-11-04）：行情改由前端在详情页
    # 按需取（腾讯 `fqkline`，CORS `*`）。跑批不再采集历史行情 → `inputs.prices`
    # 恒为空 → 本键恒不输出。保留这段只为兼容**旧产物**重放（契约 §3.4 仍是可选字段）。
    kline = derived.monthly_kline(inputs.prices.get(code) or [])
    if kline is not None:  # 可选字段：无行情时不写该键（契约 §3.4）
        payload["kline"] = kline

    pool = inputs.pools.get(code)
    payload["peers"] = (
        derived.peer_rows(pool.members, inputs.stocks_by_code, inputs.raws) if pool else []
    )
    return write_json(out_dir / "stock" / f"{code}.json", payload)


__all__ = ["build_stock"]
