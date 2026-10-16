"""跑批总入口（任务 M3-4）：一条命令跑完全链。

链路（家法 R16「离线永远可跑」）：

    采集（三级回退 → 清洗）→ 评分（engine）→ 落库（score_snapshot / meta_snapshot）
    → 产物（meta / industries / ranking / stock）→ 校验（schema + 引用完整性）

- 产物结构与口径依据《产物契约》§3（contractVersion 1.2）；校验失败**非零退出**；
- 幂等（家法 R19）：相同输入 + 相同 ``--generated-at``（含其日期作为 ``as_of``）
  ⇒ 相同产物；
- 未指定 ``--generated-at`` 时用当前 UTC 时刻，``snapshotVersion`` 取其日期。

用法：
    python -m src.batch                              # 联网跑批（AkShare → 官方源 → fixtures）
    python -m src.batch --offline                    # 只用 fixtures（CI / 本地回归）
    python -m src.batch --offline --no-store         # 不落库，只出产物
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 支持 `python src/batch.py` 直接运行（家法 R17）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.build import BuildInputs  # noqa: E402
from src.build.build_meta import (  # noqa: E402
    build_industries,
    build_meta,
    build_search_index,
    market_standard,
)
from src.build.build_ranking import build_ranking  # noqa: E402
from src.build.build_stock import build_stock  # noqa: E402
from src.build.validate import validate_products  # noqa: E402
from src.datasource.base import DataSourceError  # noqa: E402
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


def _stale_after_for(mode: str, offline: bool) -> timedelta | None:
    """模式 → 全历史数据新鲜度 TTL（D-37）；``None`` = 永不过期。

    ``cold`` 基线永不过期；``monthly`` / ``financials``（含 ``retry`` 补抓）按
    ``config.MONTHLY_FRESH_DAYS`` / ``FINANCIALS_FRESH_DAYS`` 判断 cold/ 缓存是否超龄。
    ``daily`` 不抓历史键，返回值无影响；``offline`` 不走数据源缓存。
    """
    if offline:
        return None
    if mode == "monthly":
        return timedelta(days=config.MONTHLY_FRESH_DAYS)
    if mode in {"financials", "retry"}:
        return timedelta(days=config.FINANCIALS_FRESH_DAYS)
    return None


def prepare_out_dir(out_dir: Path) -> None:
    """重置产物目录：删掉上一批的 ``ranking/`` 与 ``stock/``。

    产物 = **本次跑批的快照**；样本变小时（如 ``--codes`` 小样本）残留的分片文件会变成
    「stock/ 有而榜单未收录」的孤儿，让产物校验失败（实跑踩过）。``meta`` / ``industries`` /
    ``search-index`` 是整文件覆盖，无需处理。
    """
    for name in ("ranking", "stock"):
        target = Path(out_dir) / name
        if target.exists():
            shutil.rmtree(target)


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

    codes = list(args.codes or [])
    if args.codes_file:
        lines = [
            line.strip()
            for line in args.codes_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        codes.extend(lines)
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

    try:
        outcome = fetch.fetch_universe(
            markets=tuple(args.markets),
            codes=codes or None,
            limit=args.limit,
            chain=fetch.default_chain(
                offline=args.offline,
                fixtures_path=args.fixtures,
                # 冷启动基线：固定分区 + 永不过期（D-34/D-37）
                cache_partition="cold" if args.mode == "cold" and not args.offline else None,
                # 全历史数据新鲜度 TTL（D-37）：超龄才重抓，长跑批跨天/跨周仍可续传
                stale_after=_stale_after_for(args.mode, args.offline),
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
    prepare_out_dir(out_dir)  # 清掉上一批的分片目录，避免孤儿文件（家法 R19 幂等）
    written = [
        build_meta(inputs, out_dir),
        build_industries(inputs, out_dir),
        build_search_index(inputs, out_dir),
    ]
    for market in config.MARKETS:
        written.append(build_ranking(inputs, market, out_dir))
    for score in inputs.scored:
        written.append(build_stock(inputs, score.code, out_dir))
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
        default=None,
        help="标的清单文件（每行一个代码，# 开头为注释；与 --codes 合并去重）",
    )
    parser.add_argument("--limit", type=int, default=None, help="每个市场最多取多少只")
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    for market in args.markets:
        if market not in config.MARKETS:
            print(f"[batch][ERROR] 不支持的市场：{market}", file=sys.stderr)
            return 2
    if args.offline and not Path(args.fixtures).exists():
        print(f"[batch][ERROR] fixtures 不存在：{args.fixtures}", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
