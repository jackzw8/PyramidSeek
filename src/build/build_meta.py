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


def _universe(inputs: BuildInputs) -> dict[str, Any]:
    """样本口径回执（M5-3）：本轮实际清洗后的样本，按市场统计。"""
    by_market: dict[str, dict[str, Any]] = {}
    for stock in inputs.stocks:
        code = str(stock.get("code") or "")
        market = str(stock.get("market") or ("HK" if code.startswith("HK") else "A"))
        slot = by_market.setdefault(market, {"stocks": 0, "l1": set(), "l2": set()})
        slot["stocks"] += 1
        for level, key in (("l1", "l1_name"), ("l2", "l2_name")):
            value = str(stock.get(key) or "").strip()
            if value and value != config.INDUSTRY_UNCLASSIFIED:
                slot[level].add(value)
    return {
        "byMarket": {
            market: {
                "stocks": slot["stocks"],
                "industriesL1": len(slot["l1"]),
                "industriesL2": len(slot["l2"]),
            }
            for market, slot in sorted(by_market.items())
        },
        "industryStandard": dict(config.INDUSTRY_STANDARD),
    }


def _quality(inputs: BuildInputs) -> dict[str, Any]:
    """质量回执（M5-3）：缺财报标的与全链路失败接口，供控制台 / 前端标注。"""
    return {
        "missingFinancials": list(inputs.missing_financials),
        "failedInterfaces": list(inputs.failed_interfaces),
    }


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
        # 前端「评分构成」展示用（家法 R12：权重只从产物读，前端不复制常量）
        "weights": dict(config.WEIGHTS),
        "generatedAt": inputs.generated_at,
        "dataDate": data_date,
        "industryStandard": {market: market_standard(inputs, market) for market in config.MARKETS},
        "degraded": bool(inputs.degraded),
        "notes": "；".join(part for part in notes if part),
    }
    # 实际选源回执（M5-1 / ADR-015）：让「换源后分数变了」可追溯。
    # 字段可选：旧版产物 / 纯离线回归没有该键仍合法（contractVersion 不变）。
    if inputs.source_policy:
        payload["sourcePolicy"] = {
            "chain": inputs.source_policy.get("chain", []),
            "byMarket": inputs.source_policy.get("byMarket", {}),
        }
    # 口径与质量回执（M5-3 / ADR-014）
    payload["universe"] = _universe(inputs)
    payload["quality"] = _quality(inputs)
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


def _pinyin_letters(name: str) -> str:
    """名称 → 拼音首字母（非汉字原样保留，如 ``TCL科技`` → ``TCLkj``）。

    未安装 ``pypinyin`` 时返回空串（索引降级，前端仍可用代码 / 名称匹配）——
    延迟导入，与 ``duckdb`` / ``pymupdf`` 同一策略（家法 R16：离线也要能跑）。
    """
    try:
        from pypinyin import Style, lazy_pinyin  # noqa: PLC0415  （刻意延迟导入）
    except ImportError:
        return ""
    return "".join(lazy_pinyin(name, style=Style.FIRST_LETTER))


def build_search_index(inputs: BuildInputs, out_dir: Path) -> Path:
    """生成 ``search-index.json``（搜索索引，契约 §3.6），返回写出路径。"""
    entries = [
        {
            "code": code,
            "name": str(stock.get("name") or code),
            "market": str(stock.get("market") or ""),
            "py": _pinyin_letters(str(stock.get("name") or "")),
        }
        for code, stock in sorted(inputs.stocks_by_code.items())
    ]
    payload: dict[str, Any] = {
        "snapshotVersion": inputs.snapshot_version,
        "contractVersion": config.CONTRACT_VERSION,
        "entries": entries,
    }
    return write_json(out_dir / "search-index.json", payload)


__all__ = ["build_industries", "build_meta", "build_search_index", "market_standard"]
