"""元数据与行业树产物（任务 M3-3）。

- ``meta.json``（《产物契约》§3.1）：快照版本 / 数据日期 / 行业标准 / 降级标记 / notes；
  ``notes`` 必带免责声明（家法 R20）。
- ``industries.json``（§3.2）：市场-板块-行业树 + 各对比池样本数（含是否回退）；
  树的节点按 code 升序，池计数由 ``inputs.pools``（M2 的 :func:`engine.industry.build_pools`）
  直接派生 —— 与评分用的池**同源**，避免第二套口径。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .. import config
from . import BuildInputs, write_json


def market_standard(inputs: BuildInputs, market: str) -> str:
    """该市场**实际使用**的行业标准（取主数据里出现最多的 ``industry_std``，缺失回落 config）。"""
    counter = Counter(
        str(stock.get("industry_std"))
        for stock in inputs.stocks
        if str(stock.get("market") or "") == market and stock.get("industry_std")
    )
    if counter:
        return counter.most_common(1)[0][0]
    return config.INDUSTRY_STANDARD.get(market, "")


def build_meta(inputs: BuildInputs, out_dir: Path) -> Path:
    """生成 ``meta.json``，返回写出路径。"""
    data_date: dict[str, str] = {}
    if inputs.data_date_financial:
        data_date["financial"] = inputs.data_date_financial
    if inputs.data_date_price:  # 无行情时该键缺省（契约 §3.1）
        data_date["price"] = inputs.data_date_price

    notes = [*inputs.notes, config.DISCLAIMER]
    payload: dict[str, Any] = {
        "snapshotVersion": inputs.snapshot_version,
        "contractVersion": config.CONTRACT_VERSION,
        "formulaVersion": config.FORMULA_VERSION,
        "generatedAt": inputs.generated_at,
        "dataDate": data_date,
        "industryStandard": {market: market_standard(inputs, market) for market in config.MARKETS},
        "degraded": bool(inputs.degraded),
        "notes": "；".join(part for part in notes if part),
    }
    return write_json(out_dir / "meta.json", payload)


def build_industries(inputs: BuildInputs, out_dir: Path) -> Path:
    """生成 ``industries.json``（行业树 + 池计数），返回写出路径。"""
    tree: list[dict[str, Any]] = []
    for market in config.MARKETS:
        stocks = [s for s in inputs.stocks if str(s.get("market") or "") == market]
        l1_nodes: dict[str, str] = {}
        l2_nodes: dict[str, tuple[str, str]] = {}
        for stock in stocks:
            l1_code, l1_name = stock.get("l1_code"), stock.get("l1_name")
            l2_code, l2_name = stock.get("l2_code"), stock.get("l2_name")
            if l1_code and l1_name:
                l1_nodes[str(l1_code)] = str(l1_name)
            if l2_code and l2_name:
                l2_nodes[str(l2_code)] = (str(l2_name), str(l1_code or ""))
        tree.append(
            {
                "market": market,
                "l1": [{"code": code, "name": l1_nodes[code]} for code in sorted(l1_nodes)],
                "l2": [
                    {"code": code, "name": name, "parent": parent}
                    for code, (name, parent) in sorted(l2_nodes.items())
                ],
            }
        )

    counts: dict[str, dict[str, Any]] = {}
    for pool in inputs.pools.values():
        counts[f"{pool.market}:{pool.group}"] = {
            "market": pool.market,
            "group": pool.group,
            "level": pool.level,
            "count": pool.size,
            "use_fallback": pool.use_fallback,
        }

    payload: dict[str, Any] = {
        "snapshotVersion": inputs.snapshot_version,
        "contractVersion": config.CONTRACT_VERSION,
        "markets": list(config.MARKETS),
        "tree": tree,
        "counts": dict(sorted(counts.items())),
    }
    return write_json(out_dir / "industries.json", payload)


__all__ = ["build_industries", "build_meta", "market_standard"]
