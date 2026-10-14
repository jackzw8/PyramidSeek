"""采集编排（任务 M1-6）：三级回退 → 清洗 → 落库。

把 M1 各步串成一条链（《数据源接入规格》§1、《计划完成情况》D-11）：

    list_stocks / financials / price_daily / industry_tree
        ↓ 三级回退：主源 AkShare → 官方源（巨潮/披露易）→ fixtures（家法 R16）
    pipeline.clean.clean_universe   （校验 / 补齐 / 去重 / 窗口裁剪）
        ↓
    pipeline.store.write_standard   （DuckDB 标准层 + Parquet 冷存，幂等）

**降级必须留痕**：任何字段走到非主源即 ``degraded = True``，并把"哪个接口、由
哪个源提供、前序源为何失败"写进 ``FetchOutcome.notes``（对应产物 ``meta.degraded``）。

**小样本优先**：全量列表接口单项就要十几分钟（D-10），故提供 ``--codes`` / ``--limit``
/ ``--offline``；正式全量跑批见 M5 的定时任务。

用法：
    python -m src.pipeline.fetch --offline              # 只跑 fixtures（CI / 断网）
    python -m src.pipeline.fetch --limit 5              # 先小样本冒烟
    python -m src.pipeline.fetch --codes SH600900 --no-store
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .. import config
from ..datasource.akshare_src import AkShareSource
from ..datasource.base import DataSourceError
from ..datasource.fixtures_src import FixturesSource
from ..datasource.official_src import OfficialSource
from . import clean, store

logger = logging.getLogger(__name__)


@dataclass
class FetchOutcome:
    """采集编排的产出与留痕（M1-5/M1-6 的下游都从这里取）。"""

    universe: clean.CleanResult
    stats: store.StoreStats | None = None
    sources: dict[str, str] = field(default_factory=dict)  # 接口 → 实际提供数据的源
    fallbacks: dict[str, list[str]] = field(default_factory=dict)  # 接口 → 前序失败原因
    notes: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """是否发生降级：**任何**接口的数据不是主源提供即视为降级。"""
        return any(source != config.PRIMARY_SOURCE_NAME for source in self.sources.values())

    @property
    def failed_interfaces(self) -> list[str]:
        """最终无数据的接口（全链路失败）。"""
        return [name for name, source in self.sources.items() if source == "none"]


def build_chain(
    *,
    primary: Any | None = None,
    official: Any | None = None,
    fixtures: Any | None = None,
) -> list[Any]:
    """构造回退链：主源 → 官方源 → fixtures（``None`` 的级会被跳过）。"""
    return [source for source in (primary, official, fixtures) if source is not None]


def _try_chain(
    method: str, *args: Any, chain: list[Any], **kwargs: Any
) -> tuple[list[Any], str, list[str]]:
    """按顺序尝试回退链，返回 ``(数据, 提供数据的源名, 前序失败原因)``。

    「返回空」也按失败处理（无法与"真的没有数据"区分），并记入原因 —— 宁可多降一级，
    也不要静默产出空集。
    """
    reasons: list[str] = []
    for source in chain:
        name = getattr(source, "name", type(source).__name__)
        try:
            data = getattr(source, method)(*args, **kwargs)
        except Exception as exc:  # 任何异常都视为该源不可用，继续下一级
            reasons.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if not data:
            reasons.append(f"{name}: 返回空集")
            continue
        return list(data), name, reasons
    return [], "none", reasons


def fetch_universe(
    *,
    markets: tuple[str, ...] = config.MARKETS,
    codes: list[str] | None = None,
    limit: int | None = None,
    chain: list[Any],
    today: date | None = None,
    persist: bool = True,
    db_path: Path | None = None,
    parquet_dir: Path | None = None,
) -> FetchOutcome:
    """按回退链采集 → 清洗 → 落库。

    :param codes: 只取这些标的（小样本冒烟用）；``None`` 表示全市场。
    :param limit: 每个市场最多取多少只（``codes`` 优先级更高）。
    :param persist: 是否落库（``False`` 只做采集与清洗）。
    """
    stocks: list[dict[str, Any]] = []
    outcome = FetchOutcome(universe=clean.CleanResult())
    seen: set[str] = set()

    for market in markets:
        rows, source, reasons = _try_chain("list_stocks", market, chain=chain)
        outcome.sources[f"list_stocks:{market}"] = source
        if reasons:
            outcome.fallbacks[f"list_stocks:{market}"] = reasons
        if codes:
            wanted = {code for code in codes}
            rows = [row for row in rows if str(row.get("code")) in wanted]
            # 指定了列表里没有的代码 → 直接用其市场推断，避免"样本为空"静默通过
            for code in sorted(wanted - {str(row.get("code")) for row in rows}):
                market_of_code = "HK" if code.startswith("HK") else "A"
                if market_of_code == market:
                    rows.append({"code": code})
        elif limit is not None:
            rows = rows[:limit]
        for row in rows:
            code = str(row.get("code") or "")
            if code and code not in seen:
                seen.add(code)
                stocks.append(row)

    if not stocks:
        raise DataSourceError(
            "回退链未能取得任何股票（主源 / 官方源 / fixtures 全部失败）："
            + "；".join(f"{key} → {value}" for key, value in outcome.fallbacks.items()),
            source="fetch",
        )

    outcome.sources["list_stocks"] = outcome.sources.get("list_stocks:A", "none")

    financials: dict[str, list[dict[str, Any]]] = {}
    for row in stocks:
        code = str(row["code"])
        rows, source, reasons = _try_chain("financials", code, chain=chain)
        outcome.sources[f"financials:{code}"] = source
        if reasons:
            outcome.fallbacks[f"financials:{code}"] = reasons
        if rows:
            financials[code] = rows

    industries: dict[str, list[dict[str, Any]]] = {}
    for market in markets:
        rows, source, reasons = _try_chain("industry_tree", market, chain=chain)
        outcome.sources[f"industry_tree:{market}"] = source
        if reasons:
            outcome.fallbacks[f"industry_tree:{market}"] = reasons
        if rows:
            industries[market] = rows

    outcome.universe = clean.clean_universe(stocks, financials, None, today=today)

    if codes:
        known = {str(row.get("code")) for row in outcome.universe.stocks}
        missing = sorted({code for code in codes} - known)
        if missing:
            outcome.notes.append(f"指定标的中未进入清洗结果（必填缺失或被外键丢弃）：{missing}")

    if persist:
        outcome.stats = store.write_standard(
            outcome.universe, db_path=db_path, parquet_dir=parquet_dir
        )

    for interface, source in sorted(outcome.sources.items()):
        if source not in (config.PRIMARY_SOURCE_NAME, "none"):
            outcome.notes.append(f"{interface} 降级到 {source}")
    for interface in outcome.failed_interfaces:
        outcome.notes.append(f"{interface} 全链路失败（无数据）")
    if industries:
        outcome.notes.append(f"行业树来自 {len(industries)} 个市场（未入标准层，M2 使用）")
    if outcome.degraded:
        outcome.notes.append("产物需标 degraded=true（《数据源接入规格》§7）")

    logger.info(
        "采集完成：股票 %d（清洗后 %d）、财报 %d、降级=%s",
        len(stocks),
        len(outcome.universe.stocks),
        len(financials),
        outcome.degraded,
    )
    return outcome


def default_chain(*, offline: bool = False) -> list[Any]:
    """默认回退链；``offline=True`` 时**只用 fixtures**（不打网络）。"""
    fixtures = FixturesSource()
    if offline:
        return [fixtures]
    return [
        AkShareSource(),
        OfficialSource(),
        fixtures,
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PyramidSeek 采集编排（M1-6）")
    parser.add_argument("--offline", action="store_true", help="只用 fixtures，不打网络")
    parser.add_argument("--markets", nargs="+", default=list(config.MARKETS), help="市场列表")
    parser.add_argument("--codes", nargs="+", default=None, help="只取这些标的（小样本冒烟）")
    parser.add_argument("--limit", type=int, default=None, help="每个市场最多取多少只")
    parser.add_argument("--no-store", action="store_true", help="不落库，只做采集与清洗")
    parser.add_argument(
        "--db", type=Path, default=None, help="DuckDB 路径（默认 data/standard.duckdb）"
    )
    parser.add_argument("--parquet-dir", type=Path, default=None, help="Parquet 目录")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets = tuple(args.markets)

    for market in markets:
        if market not in config.MARKETS:
            print(f"[fetch][ERROR] 不支持的市场：{market}", file=sys.stderr)
            return 2

    print(f"[fetch] 开始 {started}｜市场={markets}｜模式={'offline' if args.offline else 'online'}")
    try:
        outcome = fetch_universe(
            markets=markets,
            codes=args.codes,
            limit=args.limit,
            chain=default_chain(offline=args.offline),
            persist=not args.no_store,
            db_path=args.db,
            parquet_dir=args.parquet_dir,
        )
    except DataSourceError as exc:
        print(f"[fetch][ERROR] {exc}", file=sys.stderr)
        return 2

    print(f"[fetch] 股票 {len(outcome.universe.stocks)}｜财报 {len(outcome.universe.financials)}")
    print(f"[fetch] 覆盖度 {outcome.universe.coverage}｜degraded={outcome.degraded}")
    if outcome.stats:
        print(f"[fetch] 落库 {outcome.stats.path}｜行数 {outcome.stats.rows}")
    for note in outcome.notes:
        print(f"[fetch] note: {note}")
    for interface, reasons in sorted(outcome.fallbacks.items()):
        for reason in reasons:
            print(f"[fetch] fallback: {interface} ← {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FetchOutcome",
    "build_chain",
    "default_chain",
    "fetch_universe",
    "main",
    "parse_args",
]
