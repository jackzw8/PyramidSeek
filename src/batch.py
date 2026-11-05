"""跑批总入口（任务 M3-4）：一条命令跑完全链。

链路（家法 R16「离线永远可跑」）：

    采集（回退链 → 清洗）→ 评分（engine）→ 落库（score_snapshot / meta_snapshot）
    → 产物（meta / industries / ranking / stock）→ 校验（schema + 引用完整性）

- 产物结构与口径依据《产物契约》§3（contractVersion 1.2）；校验失败**非零退出**；
- 幂等（家法 R19）：相同输入 + 相同 ``--generated-at``（含其日期作为 ``as_of``）
  ⇒ 相同产物；
- 未指定 ``--generated-at`` 时用当前 UTC 时刻，``snapshotVersion`` 取其日期。

用法：
    python -m src.batch                              # 联网跑批（新浪 → THS → fixtures）
    python -m src.batch --offline                    # 只用 fixtures（CI / 本地回归）
    python -m src.batch --offline --no-store         # 不落库，只出产物
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 支持 `python src/batch.py` 直接运行（家法 R17）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import common, config, net  # noqa: E402
from src.build import BuildInputs, read_json, write_json  # noqa: E402
from src.build.build_meta import (  # noqa: E402
    build_industries,
    build_meta,
    build_search_index,
    market_standard,
)
from src.build.build_ranking import build_ranking  # noqa: E402
from src.build.build_stock import build_stock  # noqa: E402
from src.build.manifest import build_manifest  # noqa: E402
from src.build.validate import validate_products  # noqa: E402
from src.datasource.base import DataSourceError  # noqa: E402
from src.datasource.registry import (  # noqa: E402
    DEFAULT_SOURCE,
    available_sources,
    validate_selection,
)
from src.engine.indicators import raw_indicators  # noqa: E402
from src.engine.industry import build_pools  # noqa: E402
from src.engine.scoring import score_universe  # noqa: E402
from src.pipeline import fetch, store  # noqa: E402

logger = logging.getLogger(__name__)


# ── 组装产物输入 ──────────────────────────────────────────────────────────
def _max_field(rows_by_code: dict[str, list[dict[str, Any]]], field: str) -> str | None:
    """所有行里某日期字段的最大值（``YYYY-MM-DD``）；无值 → ``None``。"""
    values = [
        str(row.get(field))[:10] for rows in rows_by_code.values() for row in rows if row.get(field)
    ]
    return max(values) if values else None


def assemble_inputs(
    outcome: Any,
    *,
    snapshot_version: str,
    generated_at: str,
    as_of: str | None = None,
) -> BuildInputs:
    """把采集结果（``fetch.FetchOutcome``）组装成产物层输入（含评分）。"""
    universe = outcome.universe
    stocks = [dict(stock) for stock in universe.stocks]
    financials = {str(code): list(rows) for code, rows in universe.financials.items()}
    prices = {str(code): list(rows) for code, rows in universe.prices.items()}

    scored = score_universe(stocks, financials, as_of=as_of)
    raws = {
        str(stock.get("code")): raw_indicators(
            stock, financials.get(str(stock.get("code"))), as_of=as_of
        )
        for stock in stocks
    }
    return BuildInputs(
        snapshot_version=snapshot_version,
        generated_at=generated_at,
        stocks=tuple(stocks),
        financials=financials,
        prices=prices,
        scored=tuple(scored),
        raws=raws,
        pools=build_pools(stocks),
        degraded=bool(outcome.degraded or universe.degraded),
        notes=tuple(outcome.notes),
        source_policy=dict(outcome.source_policy),
        # M5-3 质量回执：缺财报标的（financials:* 无数据）与全链路失败接口
        missing_financials=tuple(
            sorted(
                {
                    key.split(":", 1)[1]
                    for key, value in outcome.sources.items()
                    if key.startswith("financials:") and value in ("none", "fixture")
                }
            )
        ),
        failed_interfaces=tuple(outcome.failed_interfaces),
        data_date_financial=_max_field(financials, "report_period"),
        # daily 模式不抓历史行情 → 行情截止日回落到快照日期（D-32：FetchOutcome.snapshot_date）
        data_date_price=_max_field(prices, "trade_date") or outcome.snapshot_date,
    )


# ── 跑批 ─────────────────────────────────────────────────────────────────
def _write_pending(outcome: Any, generated_at: str, path: Path) -> int:
    """把本次跑批「标的级、全链路无数据」的失败写入待补清单（D-32）。

    只收 ``financials:{code}`` / ``price_daily:{code}`` 两类 —— 全市场级接口
    （list_stocks / industry_tree / market_snapshot）失败属于整批问题，不进清单。
    """
    items: list[dict[str, Any]] = []
    for key, source in outcome.sources.items():
        if source != "none":
            continue
        kind, _, code = key.partition(":")
        if kind not in {"financials", "price_daily"} or not code:
            continue
        items.append({"kind": kind, "code": code, "reasons": outcome.fallbacks.get(key, [])})
    payload = {"generated_at": generated_at, "pending": items}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items)


def _read_pending(path: Path) -> list[str]:
    """读待补清单里的标的代码（去重排序）；文件缺失或损坏返回空。"""
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return sorted(
        {str(item.get("code")) for item in payload.get("pending", []) if item.get("code")}
    )


def _stale_after_for(mode: str, offline: bool, today: date | None = None) -> timedelta | None:
    """模式 → 全历史数据新鲜度 TTL（D-37 / D-52）；``None`` = 永不过期。

    ``cold`` 基线永不过期；``monthly`` 按月 TTL；``financials`` / ``retry`` 按**披露季分档**
    （披露季 ``FINANCIALS_FRESH_DAYS`` 短、淡季 ``FINANCIALS_FRESH_DAYS_OFF_SEASON`` 长，D-52）。
    ``daily`` 不抓历史键，返回值无影响；``offline`` 不走数据源缓存。

    :param today: 判定窗口用的日期（默认取北京今天；测试注入用）。
    """
    if offline:
        return None
    if mode == "monthly":
        return timedelta(days=config.MONTHLY_FRESH_DAYS)
    if mode in {"financials", "retry"}:
        return timedelta(days=common.financials_ttl_days(today or common.beijing_today()))
    return None


def _prescreen_enabled() -> bool:
    """预筛总开关：``PYRAMID_PRESCREEN=0`` 整体关闭（回退 D-37 的纯 TTL 行为）。"""
    return os.environ.get("PYRAMID_PRESCREEN", "1") != "0"


def _prescreen_financials(
    codes: list[str], *, expected_period: str, db_path: Path | None = None
) -> tuple[list[str], int]:
    """预筛：把「标准层已有报告期已追平」的标的从待抓清单剔除（D-52）。

    动机：财报一年只变 4 次（+ 更正），跑批时大头是**没变**的票。按报告期判定比按
    mtime/TTL 判定精确得多 —— 一轮 1222 只可从 ≈4.4h 降到 ≈1.5h。

    安全失败：读库异常 / 空库 / 标的不在库里 → 一律保留，退化为全量抓取；
    只在 ``financials`` 模式生效（``retry`` 是补抓失败项，必须真抓）。

    :return: （待抓清单，跳过数）
    """
    try:
        latest = store.read_latest_report_periods(db_path=db_path)
    except Exception:  # noqa: BLE001  库损坏 / 锁冲突 → 宁可全量，也不要漏抓
        logger.warning("预筛读标准层失败，本次退化为全量抓取", exc_info=True)
        return list(codes), 0
    if not latest:
        return list(codes), 0
    kept = [code for code in codes if latest.get(code, "") < expected_period]
    return kept, len(codes) - len(kept)


def prepare_out_dir(out_dir: Path, markets: tuple[str, ...] | None = None) -> None:
    """重置产物目录。

    ``markets=None``（默认）：删掉 ``ranking/`` 与 ``stock/`` 整个目录 —— 产物 = 本次跑批的
    **完整快照**；样本变小时（如 ``--codes`` 小样本）残留的分片会变成「stock/ 有而榜单未收录」
    的孤儿，让产物校验失败（实跑踩过）。

    ``markets``（``--merge``，D-61）：**只删这些市场的分片**，保留其他市场 —— 支持
    「今天跑 A 股、明天补港股」的分批跑法。
    """
    if markets is None:
        for name in ("ranking", "stock"):
            target = Path(out_dir) / name
            if target.exists():
                shutil.rmtree(target)
        return

    ranking_dir = Path(out_dir) / "ranking"
    stock_dir = Path(out_dir) / "stock"
    for market in markets:
        (ranking_dir / f"{market}.json").unlink(missing_ok=True)
    if not stock_dir.exists():
        return
    prefixes = {"A": ("SH", "SZ", "BJ"), "HK": ("HK",)}
    wanted = tuple(prefix for market in markets for prefix in prefixes.get(market, ()))
    if not wanted:
        return
    for path in stock_dir.glob("*.json"):
        if path.stem.startswith(wanted):
            path.unlink(missing_ok=True)


def _rebase_ranking_versions(
    out_dir: Path, keep_markets: tuple[str, ...], snapshot_version: str
) -> int:
    """把保留市场的 ``ranking/{market}.json`` 版本对齐到本轮（D-61）。

    `--merge` 保留下来的榜单是上一批写的，``meta.snapshotVersion`` 还停在旧日期；
    合并后它们与新一轮同属「当前有效快照」，版本必须一致，否则 `validate_products`
    会报「snapshotVersion 不一致」。真实来源已记在 ``meta.notes``，不丢信息。
    """
    rebased = 0
    for market in keep_markets:
        path = Path(out_dir) / "ranking" / f"{market}.json"
        payload = read_json(path)
        if not payload:
            continue
        meta = dict(payload.get("meta") or {})
        if meta.get("snapshotVersion") == snapshot_version:
            continue
        meta["snapshotVersion"] = snapshot_version
        payload["meta"] = meta
        write_json(path, payload)
        rebased += 1
    return rebased


def run(args: argparse.Namespace) -> int:
    """执行完整跑批，返回进程退出码（0 成功 / 1 校验失败 / 2 采集失败）。"""
    generated_at = args.generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot_version = args.snapshot_version or generated_at[:10]
    as_of = generated_at[:10]  # 生效日截断（家法 R9）与快照同源，保可复现

    print(
        f"[batch] PS_ENV={config.PS_ENV_CI if args.offline else config.PS_ENV_LOCAL}"
        f"｜snapshotVersion={snapshot_version}｜generatedAt={generated_at}"
    )

    # 模式 → 抓取开关（D-32）；离线回归不受模式影响（fixtures 全量，保证可复现）
    if args.offline:
        with_financials, with_history, with_raw = True, True, False
    else:
        with_financials, with_history, with_raw = {
            "daily": (False, False, False),
            "monthly": (False, True, True),
            "financials": (True, False, False),
            "cold": (True, True, True),
            "retry": (True, True, False),
        }[args.mode]
        # 历史行情改前端按需取（D-56 / M4-7）：跑批一律不抓。
        # 硬闸在 config.HISTORY_FETCH_ENABLED，这里再兜一层，防止模式表被改回去误取。
        if with_history and not config.HISTORY_FETCH_ENABLED:
            with_history, with_raw = False, False
            print("[batch] 历史行情采集已停用（M4-7：详情页按需取）→ with_history=False")

    codes = list(args.codes or [])
    if args.codes_file:
        loaded = 0
        for path in args.codes_file:  # D-63：可传多份清单，合并去重
            lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            codes.extend(lines)
            loaded += 1
            print(f"[batch] 清单 {loaded}/{len(args.codes_file)}：{path}（{len(lines)} 个代码）")
        codes = list(dict.fromkeys(codes))  # 保序去重
    if args.mode == "retry" and not args.offline:
        pending_path = config.DATA_DIR / "_pending.json"
        pending_codes = _read_pending(pending_path)
        if pending_codes:
            print(f"[batch] retry：待补 {len(pending_codes)} 只（{pending_path}）")
            codes = sorted(set(codes) | set(pending_codes))
        else:
            print("[batch] retry：无待补项（_pending.json 为空或不存在）")

    print(
        f"[batch] mode={args.mode}｜with_financials={with_financials}｜with_history={with_history}"
    )

    today_cn = common.beijing_today()
    if with_financials and not args.offline:
        window = common.disclosure_window(today_cn) or "淡季"
        print(
            f"[batch] 财报缓存：{window}｜TTL {common.financials_ttl_days(today_cn)} 天"
            f"（按北京日期 {today_cn.isoformat()} 判定，D-52）"
        )

    # D-52 预筛：只对 financials 模式 + 有明确清单 + 会落库时生效（retry/daily/cold 不预筛）；
    # PYRAMID_PRESCREEN=0 可整体关闭（回退到 D-37 的纯 TTL 行为）
    skipped_codes: list[str] = []
    if (
        args.mode == "financials"
        and codes
        and not args.no_store
        and not args.offline
        and _prescreen_enabled()
    ):
        expected = common.expected_report_period(today_cn)
        kept, skipped = _prescreen_financials(codes, expected_period=expected, db_path=args.db)
        kept_set = set(kept)
        skipped_codes = [code for code in codes if code not in kept_set]
        # 注意：**codes 不缩减** —— stocks 仍要按原清单拉，行业名以本轮为准
        # （申万会改名，例如「白酒」→「白酒Ⅱ」，用旧名会让行业分位池错位）
        print(f"[batch] 预筛：应到报告期 {expected}｜跳过 {skipped} 只，待抓 {len(kept)} 只")
        if skipped_codes and not kept:
            print("[batch] 预筛：全部标的已追平，本次不抓财报（从标准层读回）")

    # 财报选源（M5-1 / ADR-015）：平权可选、按市场各选一个，失败不自动换源。
    # --offline 固定走 fixtures，不接受选源。
    selection = (
        None
        if args.offline
        else validate_selection(
            {key: value for key, value in (("A", args.source_a), ("HK", args.source_hk)) if value}
        )
    )
    if selection:
        print("[batch] 财报选源：" + "｜".join(f"{m}={s}" for m, s in sorted(selection.items())))

    try:
        outcome = fetch.fetch_universe(
            markets=tuple(args.markets),
            codes=codes or None,
            skip_financials=set(skipped_codes) or None,
            sync_lists_to_cold=(args.mode == "daily" and not args.offline),
            # D-62：daily 跑全量港股时不做行业/股本回填（666 只 ×1.2s ≈ 13 分钟），
            # 带 --codes 的小样本才补；建仓类模式（financials/cold）照旧补全。
            fill_hk_metadata=(args.mode != "daily" or bool(codes)),
            limit=args.limit,
            sources=selection,
            chain=fetch.default_chain(
                offline=args.offline,
                fixtures_path=args.fixtures,
                sources=selection,
                # 列表 / 申万映射属「时点数据」，受本参数影响；若按日分区则每天首次跑批
                # 都会重建 131 个行业成分（≈15~30 分钟）。建仓类模式一律复用 cold/ 固定
                # 分区，避免切模式白等一輪（D-43）。只有 daily 走按日分区，取最新列表/快照。
                cache_partition=(None if args.mode == "daily" or args.offline else "cold"),
                # 全历史数据新鲜度 TTL（D-37 / D-52）：超龄才重抓，长跑批跨天/跨周仍可续传
                stale_after=_stale_after_for(args.mode, args.offline, today_cn),
            ),
            today=date.fromisoformat(as_of),
            persist=not args.no_store,
            db_path=args.db,
            parquet_dir=args.parquet_dir,
            with_raw=with_raw,
            with_financials=with_financials,
            with_history=with_history,
        )
    except DataSourceError as exc:
        print(f"[batch][ERROR] 采集失败：{exc}", file=sys.stderr)
        return 2

    inputs = assemble_inputs(
        outcome, snapshot_version=snapshot_version, generated_at=generated_at, as_of=as_of
    )
    if outcome.stats:
        print(f"[batch] 标准层落库 {outcome.stats.path}（{outcome.stats.rows}）")
    print(
        f"[batch] 股票 {len(inputs.stocks)}｜评分 {len(inputs.scored)}｜"
        f"degraded={inputs.degraded}｜as_of={as_of}"
    )
    for note in inputs.notes:
        print(f"[batch] note: {note}")

    if not args.no_store:
        stats = store.write_analytics(
            inputs.scored,
            snapshot_version=snapshot_version,
            meta={
                "data_date_financial": inputs.data_date_financial,
                "data_date_price": inputs.data_date_price,
                "industry_std": "；".join(
                    f"{market}={market_standard(inputs, market)}" for market in config.MARKETS
                ),
                "formula_version": config.FORMULA_VERSION,
                "degraded": inputs.degraded,
                "notes": "；".join(inputs.notes),
            },
            db_path=args.db,
        )
        print(f"[batch] 分析层落库 {stats.path}（{stats.rows}）")

    out_dir = Path(args.out)
    # D-61：--merge 只替换本轮市场的分片，其余市场沿用上一批（支持分批跑）
    merge_markets = tuple(args.markets) if args.merge else None
    prepare_out_dir(out_dir, merge_markets)
    written = [
        build_meta(inputs, out_dir, merge_markets=merge_markets),
        build_industries(inputs, out_dir, merge_markets=merge_markets),
        build_search_index(inputs, out_dir, merge_markets=merge_markets),
    ]
    # 只为本轮市场写榜单：`build_ranking` 对没数据的市场也会写空数组，
    # merge 模式下会把其他市场的榜单覆盖成空（实测踩过）。
    for market in merge_markets or tuple(config.MARKETS):
        written.append(build_ranking(inputs, market, out_dir))
    for score in inputs.scored:
        written.append(build_stock(inputs, score.code, out_dir))
    if merge_markets:
        kept = tuple(market for market in config.MARKETS if market not in merge_markets)
        rebased = _rebase_ranking_versions(out_dir, kept, snapshot_version)
        if rebased:
            print(f"[batch] 合并：{rebased} 个保留市场的榜单版本对齐到 {snapshot_version}")
    # manifest 最后生成：扫描全目录（此时 stock/*.json 才齐全），否则会漏文件
    written.append(build_manifest(inputs, out_dir))
    print(f"[batch] 产物 {len(written)} 个 → {out_dir}")

    if not args.offline:
        pending_path = config.DATA_DIR / "_pending.json"
        pending_count = _write_pending(outcome, generated_at, pending_path)
        if pending_count:
            print(f"[batch] 待补清单：{pending_count} 项 → {pending_path}（--mode retry 补抓）")

    if not args.no_validate:
        problems = validate_products(out_dir)
        if problems:
            for problem in problems:
                print(f"[batch][ERROR] {problem}", file=sys.stderr)
            print(f"[batch] 产物校验失败：{len(problems)} 个问题", file=sys.stderr)
            return 1
        print("[batch] 产物校验通过（schema + 引用完整性）")

    print(f"[batch] 完成：{config.DISCLAIMER}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PyramidSeek 跑批入口（M3-4）")
    parser.add_argument(
        "--mode",
        choices=("daily", "monthly", "financials", "cold", "retry"),
        default="daily",
        help=(
            "跑批模式（D-32）：daily=快照+重算（默认，分钟级）；monthly=+行情历史重抓"
            "（含 raw 以补 hfq）；financials=+三表全量（披露季）；cold=三表+行情历史（冷启动）；"
            "retry=补上次 _pending.json 清单。--offline 时不生效（fixtures 全量，回归可复现）"
        ),
    )
    parser.add_argument("--offline", action="store_true", help="只用 fixtures，不打网络")
    parser.add_argument("--markets", nargs="+", default=list(config.MARKETS), help="市场列表")
    parser.add_argument("--codes", nargs="+", default=None, help="只跑这些标的（小样本冒烟）")
    parser.add_argument(
        "--codes-file",
        type=Path,
        action="append",
        default=None,
        help="标的清单文件（每行一个代码，# 开头为注释；与 --codes 合并去重）",
    )
    parser.add_argument("--limit", type=int, default=None, help="每个市场最多取多少只")
    parser.add_argument(
        "--merge",
        action="store_true",
        help="分市场合并：只替换本轮市场的产物，保留其他市场（D-61，支持分批跑）",
    )
    parser.add_argument(
        "--source-a",
        choices=available_sources("A"),
        default=None,
        help=f"A 股财报源（平权可选，默认 {DEFAULT_SOURCE['A']}）",
    )
    parser.add_argument(
        "--source-hk",
        choices=available_sources("HK"),
        default=None,
        help=f"港股财报源（平权可选，默认 {DEFAULT_SOURCE['HK']}）",
    )
    parser.add_argument("--fixtures", type=Path, default=config.FIXTURES_FILE, help="fixtures 路径")
    parser.add_argument("--out", type=Path, default=config.PUBLIC_DATA_DIR, help="产物输出目录")
    parser.add_argument(
        "--snapshot-version", default=None, help="快照版本（默认取 --generated-at 的日期）"
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="生成时间戳（UTC ISO8601）；固定该值即可复现整套产物（R19）",
    )
    parser.add_argument("--no-store", action="store_true", help="不落库（标准层 + 分析层）")
    parser.add_argument("--db", type=Path, default=None, help="DuckDB 路径")
    parser.add_argument("--parquet-dir", type=Path, default=None, help="Parquet 目录")
    parser.add_argument("--no-validate", action="store_true", help="跳过产物校验")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args(argv)

    for market in args.markets:
        if market not in config.MARKETS:
            print(f"[batch][ERROR] 不支持的市场：{market}", file=sys.stderr)
            return 2
    if args.offline and not Path(args.fixtures).exists():
        print(f"[batch][ERROR] fixtures 不存在：{args.fixtures}", file=sys.stderr)
        return 2
    if not args.offline:
        # D-46：本机 IPv6「有前缀没出口」时，每个 AAAA 地址建连白等 21s
        # （一张财报 44s）。探测一次，不通就本进程只用 IPv4；PYRAMID_IPV6=0/1 可强制。
        net.ensure_ipv4_preferred()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
