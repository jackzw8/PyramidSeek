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
    # 全市场快照成功时的跑批日（ISO）：daily 模式不抓历史行情 → 行情截止日由快照提供（D-32）
    snapshot_date: str | None = None

    @property
    def degraded(self) -> bool:
        """是否发生降级：**有数据的**接口只要不是主源提供即视为降级。

        例外：行情（``price_daily:*``）**全链路无数据不算降级** —— 产物契约里 ``kline``
        是可选字段（§3.4），缺行情只影响月K 卡片，不代表数据源降级（2026-10-15 实跑：
        fixtures 不含行情、部分港股无行情）。
        """
        for name, source in self.sources.items():
            if source == "none":
                # 行情与全市场快照都是「增强」接口：缺了不影响主榜与评分的正确性
                # （快照缺失只是少一个停牌判定维度，且会留 note 提示），不算降级。
                if name.startswith("price_daily") or name.startswith("market_snapshot"):
                    continue
                return True
            if source != config.PRIMARY_SOURCE_NAME:
                return True
        return False

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
    with_raw: bool = False,
    with_financials: bool = True,
    with_history: bool = True,
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
    # 模式开关（D-32）：`daily` 模式不抓三表 —— 财报来自**标准层落库**（上一次
    # `financials`/`cold` 模式的结果，按日缓存的标准行），本次只补快照；
    # 读回失败（空库 / 未跑过三表）→ 财报为空，产物缺评分 —— daily 的前置条件
    # 是先跑过一次带落库的三表。
    if with_financials:
        for row in stocks:
            code = str(row["code"])
            rows, source, reasons = _try_chain("financials", code, chain=chain)
            outcome.sources[f"financials:{code}"] = source
            if reasons:
                outcome.fallbacks[f"financials:{code}"] = reasons
            if rows:
                financials[code] = rows
    else:
        try:
            stored = store.read_table("financial_report", db_path=db_path)
        except Exception:  # noqa: BLE001  空库 / 表不存在 → 财报为空
            stored = []
        for row in stored:
            code = str(row.get("code") or "")
            if code:
                financials.setdefault(code, []).append(row)

    # 行情（D-32 决策 1/2）：**默认只取前复权**（1 次/只）—— `adjust="qfq"` 返回的 OHLC
    # 本身就是前复权价，`clean.merge_price_rows` 走 qfq-only 形态（B 级降级：无 `adj_factor`，
    # 产物 `kline` 照常出、`hfq` 省略）；`with_raw=True`（monthly/cold 模式）时额外取
    # 「不复权」并合并反算 `adj_factor`，以输出 `hfq`。两道都拿不到才当该股无行情。
    # `with_history=False`（daily 模式）不抓历史序列 —— 行情截止日由快照提供。
    prices: dict[str, list[dict[str, Any]]] = {}
    price_issues: list[Any] = []
    for row in stocks if with_history else []:
        code = str(row["code"])
        raw_rows: list[dict[str, Any]] = []
        if with_raw:
            raw_rows, source_raw, reasons_raw = _try_chain(
                "price_daily", code, chain=chain, adjust="raw"
            )
            outcome.sources[f"price_daily_raw:{code}"] = source_raw
            if reasons_raw:
                outcome.fallbacks[f"price_daily_raw:{code}"] = reasons_raw
        qfq_rows, source_qfq, reasons_qfq = _try_chain(
            "price_daily", code, chain=chain, adjust=config.DEFAULT_ADJUST
        )
        outcome.sources[f"price_daily:{code}"] = source_qfq
        if reasons_qfq:
            outcome.fallbacks[f"price_daily:{code}"] = reasons_qfq
        if not qfq_rows:
            continue
        merged, issues = clean.merge_price_rows(code, raw_rows, qfq_rows)
        price_issues.extend(issues)
        if merged:
            prices[code] = merged

    # 全市场快照（D-32）：1 次调用覆盖全市场 —— 行情截止日 + 停牌判定 + 最新价。
    # 失败不阻塞（降级：无停牌判定；行情截止日回落到历史行情的最大交易日）。
    snapshots: dict[str, dict[str, dict[str, Any]]] = {}
    snapshot_date: str | None = None
    for market in markets:
        snap_rows, snap_source, snap_reasons = _try_chain("market_snapshot", market, chain=chain)
        outcome.sources[f"market_snapshot:{market}"] = snap_source
        if snap_reasons:
            outcome.fallbacks[f"market_snapshot:{market}"] = snap_reasons
        if not snap_rows:
            outcome.notes.append(
                f"market_snapshot:{market} 全链路失败（无停牌判定，停牌股不会被单列）"
            )
            continue
        snapshots[market] = {row["code"]: row for row in snap_rows}
        if snapshot_date is None:
            snapshot_date = today.isoformat() if today else date.today().isoformat()

    # 停牌判定（闭环 M1 遗留 TODO）：快照里「最新价」缺失 → suspended。
    # 只对 status=normal 的标的打标 —— ST / 退市 / 次新等既成事实不覆盖。
    if snapshots:
        suspended = {
            code
            for snap in snapshots.values()
            for code, snap_row in snap.items()
            if snap_row.get("latest_price") is None
        }
        flagged = 0
        for stock in stocks:
            code = str(stock.get("code") or "")
            if code in suspended and stock.get("status") == "normal":
                stock["status"] = "suspended"
                flagged += 1
        if flagged:
            outcome.notes.append(
                f"快照停牌判定：{flagged} 只标的最新价缺失 → 标记 suspended（单列）"
            )

    outcome.snapshot_date = snapshot_date

    industries: dict[str, list[dict[str, Any]]] = {}
    for market in markets:
        rows, source, reasons = _try_chain("industry_tree", market, chain=chain)
        outcome.sources[f"industry_tree:{market}"] = source
        if reasons:
            outcome.fallbacks[f"industry_tree:{market}"] = reasons
        if rows:
            industries[market] = rows

    outcome.universe = clean.clean_universe(stocks, financials, prices, today=today)
    outcome.universe.issues.extend(price_issues)

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


def default_chain(
    *,
    offline: bool = False,
    fixtures_path: Path | None = None,
    cache_partition: str | None = None,
) -> list[Any]:
    """默认回退链；``offline=True`` 时**只用 fixtures**（不打网络）。

    :param cache_partition: 透传给各数据源的缓存分区（D-34）：``"cold"`` 用于
        冷启动跑批的固定缓存目录（跨天续传）；``None`` = 按日分区（日常）。
    """
    fixtures = FixturesSource(fixtures_path)
    if offline:
        return [fixtures]
    return [
        AkShareSource(partition=cache_partition),
        OfficialSource(partition=cache_partition),
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
