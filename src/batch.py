"""批处理总入口（任务 M0-1 / M3-4）。

M0 阶段行为：读取 ``fixtures/`` 示例数据，输出**占位产物**到 ``public/data/``
（``meta.json`` / ``industries.json`` / ``ranking/{A,HK}.json``），
用于验证「读 fixtures → 出产物」链路与契约形状。

- 评分引擎未实现 → 产物中 ``score/quality/growth = 0``，``meta.notes`` 已标注；
- 正式数值由 M2（评分引擎）与 M3（产物层）落位；
- 幂等：相同输入 + 相同 ``snapshotVersion`` ⇒ 相同产物（``generatedAt`` 为时间戳，
  需要严格可复现时用 ``--generated-at`` 固定）。

用法：
    python -m src.batch
    python -m src.batch --fixtures fixtures/financials.json --out public/data
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 支持 `python src/batch.py` 直接运行（家法 R17）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

# 排行榜列顺序声明（《产物契约》§3.3）
RANKING_COLUMNS = [
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
    "isTop10pct",
]


# ── 读取与归组 ────────────────────────────────────────────────────────────
def load_fixtures(path: Path) -> dict[str, Any]:
    """读取 fixtures 示例数据。"""
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_pools(universe: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """解析每只股票的对比池（二级样本 < 30 回退一级，家法 R6）。"""
    info: dict[str, dict[str, Any]] = {}
    for market in config.MARKETS:
        stocks = [s for s in universe if s["market"] == market]
        l2_groups: dict[str, list[dict[str, Any]]] = {}
        l1_groups: dict[str, list[dict[str, Any]]] = {}
        for s in stocks:
            l2_groups.setdefault(s["l2_name"], []).append(s)
            l1_groups.setdefault(s["l1_name"], []).append(s)
        for s in stocks:
            fallback = len(l2_groups[s["l2_name"]]) < config.INDUSTRY_FALLBACK_MIN
            info[s["code"]] = {
                "level": (
                    config.COMPARE_LEVEL_FALLBACK if fallback else config.COMPARE_LEVEL_DEFAULT
                ),
                "group": s["l1_name"] if fallback else s["l2_name"],
                "count": len(l1_groups[s["l1_name"]]) if fallback else len(l2_groups[s["l2_name"]]),
                "use_fallback": fallback,
            }
    return info


# ── 产物构建（M0 占位实现）────────────────────────────────────────────────
def build_meta(universe: list[dict[str, Any]], snapshot_version: str, generated_at: str) -> dict:
    """构建 meta.json 占位产物（正式实现见 M3-3）。"""
    periods = [fin["report_period"] for s in universe for fin in s.get("financials", {}).values()]
    return {
        "snapshotVersion": snapshot_version,
        "contractVersion": config.CONTRACT_VERSION,
        "formulaVersion": config.FORMULA_VERSION,
        "generatedAt": generated_at,
        "dataDate": {"financial": max(periods) if periods else None},
        "industryStandard": dict(config.INDUSTRY_STANDARD),
        "degraded": False,
        "notes": (
            "M0 占位产物：评分引擎未实现（score/quality/growth = 0）；"
            f"数据来自 fixtures；{config.DISCLAIMER}"
        ),
    }


def build_industries(
    universe: list[dict[str, Any]],
    pools: dict[str, dict[str, Any]],
    snapshot_version: str,
) -> dict:
    """构建 industries.json 占位产物（正式实现见 M3-3）。"""
    tree = []
    counts: dict[str, dict[str, Any]] = {}
    for market in config.MARKETS:
        stocks = [s for s in universe if s["market"] == market]
        l1_map: dict[str, str] = {}
        l2_map: dict[str, tuple[str, str]] = {}
        for s in stocks:
            l1_map.setdefault(s["l1_code"], s["l1_name"])
            l2_map.setdefault(s["l2_code"], (s["l2_name"], s["l1_code"]))
        tree.append(
            {
                "market": market,
                "l1": [{"code": c, "name": n} for c, n in l1_map.items()],
                "l2": [{"code": c, "name": n, "parent": p} for c, (n, p) in l2_map.items()],
            }
        )
        for s in stocks:
            info = pools[s["code"]]
            key = f"{market}:{info['group']}"
            counts[key] = {
                "market": market,
                "group": info["group"],
                "level": info["level"],
                "count": info["count"],
                "use_fallback": info["use_fallback"],
            }
    return {
        "snapshotVersion": snapshot_version,
        "contractVersion": config.CONTRACT_VERSION,
        "markets": list(config.MARKETS),
        "tree": tree,
        "counts": counts,
    }


def build_ranking(
    universe: list[dict[str, Any]],
    pools: dict[str, dict[str, Any]],
    market: str,
    snapshot_version: str,
) -> dict:
    """构建单市场排行榜占位产物（正式实现见 M3-1）。"""
    rows = []
    for idx, s in enumerate((x for x in universe if x["market"] == market), start=1):
        info = pools[s["code"]]
        rows.append(
            {
                "rank": idx,
                "code": s["code"],
                "name": s["name"],
                "market": market,
                "l1": s["l1_name"],
                "l2": s["l2_name"],
                "group": info["group"],
                "level": info["level"],
                "score": 0.0,
                "quality": 0.0,
                "growth": 0.0,
                "roe": 0.0,
                "gross_margin": 0.0,
                "isTop10pct": False,
            }
        )
    return {
        "meta": {
            "snapshotVersion": snapshot_version,
            "contractVersion": config.CONTRACT_VERSION,
        },
        "market": market,
        "columns": list(RANKING_COLUMNS),
        "rows": rows,
    }


# ── 写出 ─────────────────────────────────────────────────────────────────
def write_json(path: Path, payload: Any) -> Path:
    """写出 JSON（UTF-8、缩进 2、保留中文、结尾换行）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def run(
    fixtures_path: Path,
    out_dir: Path,
    snapshot_version: str,
    generated_at: str,
) -> list[Path]:
    """执行 M0 占位跑批，返回写出的产物路径列表。"""
    fixture = load_fixtures(fixtures_path)
    universe = fixture["stocks"]
    pools = resolve_pools(universe)

    written = [
        write_json(out_dir / "meta.json", build_meta(universe, snapshot_version, generated_at)),
        write_json(
            out_dir / "industries.json",
            build_industries(universe, pools, snapshot_version),
        ),
    ]
    for market in config.MARKETS:
        written.append(
            write_json(
                out_dir / "ranking" / f"{market}.json",
                build_ranking(universe, pools, market, snapshot_version),
            )
        )
    return written


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PyramidSeek 跑批入口（M0 占位产物）")
    parser.add_argument(
        "--fixtures", type=Path, default=config.FIXTURES_FILE, help="fixtures 数据文件"
    )
    parser.add_argument("--out", type=Path, default=config.PUBLIC_DATA_DIR, help="产物输出目录")
    parser.add_argument(
        "--snapshot-version",
        default=datetime.now(UTC).strftime("%Y-%m-%d"),
        help="快照版本（默认 UTC 当日）",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="生成时间戳（UTC ISO8601）；需要可复现产物时固定该值",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    generated_at = args.generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = os.environ.get("PS_ENV", config.PS_ENV_LOCAL)

    print(f"[PyramidSeek] PS_ENV={env}")
    print(f"[PyramidSeek] fixtures = {args.fixtures}")
    print(f"[PyramidSeek] snapshotVersion = {args.snapshot_version}")

    if not args.fixtures.exists():
        print(f"[PyramidSeek][ERROR] fixtures 不存在：{args.fixtures}", file=sys.stderr)
        return 2

    written = run(args.fixtures, args.out, args.snapshot_version, generated_at)
    for path in written:
        print(f"[PyramidSeek] 已写出 {path}")
    print(f"[PyramidSeek] 完成（M0 占位产物，共 {len(written)} 个）：{config.DISCLAIMER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
