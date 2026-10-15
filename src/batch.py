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
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 支持 `python src/batch.py` 直接运行（家法 R17）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.build import BuildInputs  # noqa: E402
from src.build.build_meta import build_industries, build_meta, market_standard  # noqa: E402
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
        data_date_price=_max_field(prices, "trade_date"),
    )


# ── 跑批 ─────────────────────────────────────────────────────────────────
def run(args: argparse.Namespace) -> int:
    """执行完整跑批，返回进程退出码（0 成功 / 1 校验失败 / 2 采集失败）。"""
    generated_at = args.generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot_version = args.snapshot_version or generated_at[:10]
    as_of = generated_at[:10]  # 生效日截断（家法 R9）与快照同源，保可复现

    print(
        f"[batch] PS_ENV={config.PS_ENV_CI if args.offline else config.PS_ENV_LOCAL}"
        f"｜snapshotVersion={snapshot_version}｜generatedAt={generated_at}"
    )

    try:
        outcome = fetch.fetch_universe(
            markets=tuple(args.markets),
            codes=args.codes,
            limit=args.limit,
            chain=fetch.default_chain(offline=args.offline, fixtures_path=args.fixtures),
            today=date.fromisoformat(as_of),
            persist=not args.no_store,
            db_path=args.db,
            parquet_dir=args.parquet_dir,
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
    written = [build_meta(inputs, out_dir), build_industries(inputs, out_dir)]
    for market in config.MARKETS:
        written.append(build_ranking(inputs, market, out_dir))
    for score in inputs.scored:
        written.append(build_stock(inputs, score.code, out_dir))
    print(f"[batch] 产物 {len(written)} 个 → {out_dir}")

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
    parser.add_argument("--offline", action="store_true", help="只用 fixtures，不打网络")
    parser.add_argument("--markets", nargs="+", default=list(config.MARKETS), help="市场列表")
    parser.add_argument("--codes", nargs="+", default=None, help="只跑这些标的（小样本冒烟）")
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
