"""采集编排（任务 M1-6）：回退链 → 清洗 → 落库。

把 M1 各步串成一条链（《数据源接入规格》§1、《计划完成情况》D-11）：

    list_stocks / financials / price_daily / industry_tree
        ↓ 回退链：新浪主源 → 同花顺 THS（财报第 2 级）→ fixtures（R16）
          （官方源已移出链、仅对账，D-45）
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
from ..datasource.base import DataSourceError
from ..datasource.fixtures_src import FixturesSource
from ..datasource.registry import build_source, validate_selection
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
    # M5-1：本轮实际选源（供产物 meta.sourcePolicy 留痕，ADR-015）
    source_policy: dict[str, Any] = field(default_factory=dict)
    # 主链首个源名 = 本轮「期望源」；非它提供即视为降级（保持 D-8 的降级语义）
    expected_source: str = ""
    # 全市场快照成功时的跑批日（ISO）：daily 模式不抓历史行情 → 行情截止日由快照提供（D-32）
    snapshot_date: str | None = None

    @property
    def degraded(self) -> bool:
        """是否发生降级：有数据的接口不是**本轮所选源**提供，或最终无数据。

        ADR-015 后选源是显式参数，正常路径下所有接口都由同一个源提供；测试用的
        多级 stub 链仍靠 ``expected_source`` 判断谁算「降级」。
        行情 / 全市场快照是增强接口，缺失不影响主榜与评分，豁免。
        """
        for name, source in self.sources.items():
            enhanced = name.startswith(("price_daily", "market_snapshot"))
            if source in ("none", "fixture"):
                if not enhanced:
                    return True
                continue
            if self.expected_source and source != self.expected_source and not enhanced:
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
    """构造回退链（``None`` 的级会被跳过）；完整四级链见 :func:`default_chain`
    （含 THS 第 2 级）。"""
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
    skip_financials: set[str] | None = None,
    limit: int | None = None,
    chain: list[Any] | None = None,
    sources: dict[str, str] | None = None,
    cache_partition: str | None = None,
    stale_after: Any | None = None,
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
    :param skip_financials: 这些标的**不重抓财报**（预筛已追平），但仍留在 ``stocks`` 里
        —— 列表 / 行业信息要用**本轮的**（申万行业名会改名，例如「白酒」→「白酒Ⅱ」），
        只有财报走标准层读回。``codes`` 因此不要为预筛缩减。
    :param limit: 每个市场最多取多少只（``codes`` 优先级更高）。
    :param persist: 是否落库（``False`` 只做采集与清洗）。
    """
    # 历史行情改前端按需取（D-56 / M4-7）：硬闸放在这里，任何调用方都绕不过去。
    # `market_snapshot`（停牌判定 / dataDate.price）不受影响，仍然照常采集。
    if with_history and not config.HISTORY_FETCH_ENABLED:
        with_history, with_raw = False, False

    # 主链 vs 财报链（M5-1 / ADR-015）：主链负责列表/快照/行情/行业树；
    # 财报按市场各选一个源，**链里只有它自己** → 失败不会被静默换成别的源。
    if chain is None:
        chain = default_chain(
            cache_partition=cache_partition, stale_after=stale_after, sources=sources
        )
    fin_chains = financial_chains(
        markets=markets,
        cache_partition=cache_partition,
        stale_after=stale_after,
        sources=sources,
        fallback_chain=chain,
    )
    outcome = FetchOutcome(universe=clean.CleanResult())
    outcome.expected_source = getattr(chain[0], "name", "") if chain else ""
    outcome.source_policy = {
        "chain": list(
            dict.fromkeys(
                getattr(c, "name", type(c).__name__) for fc in fin_chains.values() for c in fc
            )
        ),
        "byMarket": (validate_selection(sources) if sources else {}),
    }

    stocks: list[dict[str, Any]] = []
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
            "回退链未能取得任何股票（新浪主源 / 同花顺 THS / fixtures 全部失败）："
            + "；".join(f"{key} → {value}" for key, value in outcome.fallbacks.items()),
            source="fetch",
        )

    # 注意：不要在这里补一个裸的 ``sources["list_stocks"]`` 汇总键。原先它取
    # ``sources.get("list_stocks:A", "none")``，``--markets HK`` 时该键恒为 "none"，
    # 而 ``degraded`` 只豁免行情/快照 → 只跑港股必然误报 degraded=True，
    # 并把「产物需标 degraded」的 note 写进产物（PL-03）。按市场分的键已足以表达来源。

    financials: dict[str, list[dict[str, Any]]] = {}
    # 模式开关（D-32）：`daily` 模式不抓三表 —— 财报来自**标准层落库**（上一次
    # `financials`/`cold` 模式的结果，按日缓存的标准行），本次只补快照；
    # 读回失败（空库 / 未跑过三表）→ 财报为空，产物缺评分 —— daily 的前置条件
    # 是先跑过一次带落库的三表。
    if with_financials:
        total = len(stocks)
        for idx, row in enumerate(stocks, start=1):
            code = str(row["code"])
            if skip_financials and code in skip_financials:
                # 预筛已追平：不重抓，财报由调用方从标准层读回（保留 stocks 以取本轮行业名）
                logger.info(
                    "财报 [%d/%d] %s %s：跳过（预筛已追平）",
                    idx,
                    total,
                    code,
                    row.get("name") or "",
                )
                continue
            code_market = "HK" if code.upper().startswith("HK") else "A"
            rows, source, reasons = _try_chain(
                "financials", code, chain=fin_chains.get(code_market, chain)
            )
            outcome.sources[f"financials:{code}"] = source
            if reasons:
                outcome.fallbacks[f"financials:{code}"] = reasons
            if rows:
                financials[code] = rows
            logger.info(
                "财报 [%d/%d] %s %s：%d 行（%s）",
                idx,
                total,
                code,
                row.get("name") or "",
                len(rows),
                source,
            )
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
    price_total = len(stocks) if with_history else 0
    for idx, row in enumerate(stocks if with_history else [], start=1):
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
        logger.info(
            "行情 [%d/%d] %s %s：qfq %d 行%s（%s）",
            idx,
            price_total,
            code,
            row.get("name") or "",
            len(qfq_rows),
            f" + raw {len(raw_rows)} 行" if with_raw else "",
            source_qfq,
        )
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
        logger.info("快照 %s：%d 行（%s）", market, len(snap_rows), snap_source)
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
        if source == "fixture":
            outcome.notes.append(f"{interface} 降级到 fixtures（离线示例数据）")
        elif outcome.expected_source and source not in ("none", outcome.expected_source):
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
    stale_after: Any | None = None,
    sources: dict[str, str] | None = None,
) -> list[Any]:
    """**主链**（列表 / 快照 / 行情 / 行业树）：只放一个源 + fixtures 兜底。

    M5-1 / ADR-015：财报源由 :func:`financial_chains` 按市场单独挑，不在主链里做回退。
    主链仍带 fixtures 仅为兼容「列表接口整体挂掉」的离线可跑性（R16），且会被
    ``FetchOutcome.degraded`` 明确标记，**不是静默换源**。

    :param sources: ``{"A": "em", "HK": "em"}``；``None`` = 用注册表默认值。
    """
    fixtures = FixturesSource(fixtures_path)
    if offline:
        return [fixtures]
    selection = validate_selection(sources)
    return [
        build_source("A", selection["A"], partition=cache_partition, stale_after=stale_after),
        fixtures,
    ]


def financial_chains(
    *,
    markets: tuple[str, ...],
    cache_partition: str | None = None,
    stale_after: Any | None = None,
    sources: dict[str, str] | None = None,
    fallback_chain: list[Any] | None = None,
) -> dict[str, list[Any]]:
    """按市场构造**单源**财报链：失败即失败，不自动换源（ADR-015 / M5-1）。

    未显式给 ``sources`` 时（如旧测试直接传 ``chain=[stub]``），沿用 ``fallback_chain``，
    保持既有调用完全兼容。
    """
    if sources is None:
        chain = fallback_chain or []
        return {market: chain for market in markets}
    selection = validate_selection(sources)
    return {
        market: [
            build_source(
                market,
                selection[market],
                partition=cache_partition,
                stale_after=stale_after,
            )
        ]
        for market in markets
    }


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
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
