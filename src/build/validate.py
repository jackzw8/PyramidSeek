"""产物校验（任务 M3-5；依据《产物契约》§6）。

校验项（任一不通过 → :func:`validate_products` 返回非空问题列表 → ``main`` **非零退出**，
让 CI 拦住坏产物）：

1. **schema 合规**：``meta.json`` / ``ranking/*.json`` / ``stock/*.json`` 逐文件对照
   ``schema/*.schema.json``（Draft 2020-12）；
2. **引用完整性**：``rows`` + ``excluded`` 的每个 code 都有 ``stock/{code}.json``，
   且 ``stock/`` 下没有孤儿文件；文件名与内嵌 ``code`` 一致；
3. **rank 连续性**：各市场 ``rows`` 的 rank 为 1..n、与文件顺序一致；
4. **数值范围**：``qualitySubs`` / ``radar`` ∈ [0, 100]（schema 未覆盖的子分范围）；
5. **版本一致**：``ranking/*`` 与 ``industries.json`` 的版本字段与 ``meta.json`` 一致；
6. **主榜互斥**：同一 code 不同时出现在 ``rows`` 与 ``excluded``；
7. **结构一致性**：``years`` 与四张财务表的每条序列等长（契约 §3.4）。

用法：
    python -m src.build.validate [--data public/data] [--schema schema]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema

from .. import config

VERSION_KEYS = ("snapshotVersion", "contractVersion")
TABLE_KEYS = ("profitability", "structure", "operation", "cashflow")


# ── 基础读写 ─────────────────────────────────────────────────────────────
def _read_json(path: Path) -> tuple[Any, list[str]]:
    if not path.exists():
        return None, [f"缺少产物文件：{path}"]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"{path.name}: 读取失败（{exc}）"]


def _check_schema(data: Any, schema_path: Path, label: str) -> list[str]:
    if not schema_path.exists():
        return [f"{label}: schema 文件缺失（{schema_path}）"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{label}: schema 校验失败 {list(error.absolute_path)} → {error.message}"
        for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    ]


# ── 逐类校验 ─────────────────────────────────────────────────────────────
def _check_versions(holder: dict[str, Any], label: str, meta: dict[str, Any]) -> list[str]:
    """版本字段与 ``meta.json`` 一致（契约 §6 项 5）；``holder`` 为承载版本键的对象。"""
    problems: list[str] = []
    for key in VERSION_KEYS:
        if holder.get(key) != meta.get(key):
            problems.append(
                f"{label}: {key} = {holder.get(key)!r} 与 meta.json 的 {meta.get(key)!r} 不一致"
            )
    return problems


def _check_ranking(data: dict[str, Any], label: str, meta: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    rows = data.get("rows") or []
    excluded = data.get("excluded") or []

    ranks = [row.get("rank") for row in rows]
    if ranks != list(range(1, len(ranks) + 1)):
        problems.append(f"{label}: rank 不是 1..n 连续递增：{ranks[:10]}")

    overlap = {row.get("code") for row in rows} & {row.get("code") for row in excluded}
    if overlap:
        problems.append(f"{label}: code 同时出现在 rows 与 excluded：{sorted(overlap)}")

    problems.extend(_check_versions(data.get("meta") or {}, f"{label} · meta", meta))
    return problems


def _check_scores(payload: dict[str, Any], label: str) -> list[str]:
    problems: list[str] = []
    for section, key in (("qualitySubs", "子分"), ("radar", "雷达")):
        values = payload.get(section) or {}
        for name, value in values.items():
            if value is None:
                continue
            if not (config.SUB_SCORE_MIN <= float(value) <= config.SUB_SCORE_MAX):
                problems.append(f"{label}: {section}.{name}（{key}）超出 [0,100]：{value}")
    return problems


def _check_tables(payload: dict[str, Any], label: str) -> list[str]:
    problems: list[str] = []
    length = len(payload.get("years") or [])
    for table in TABLE_KEYS:
        columns = payload.get(table)
        if not isinstance(columns, dict):
            problems.append(f"{label}: 缺少 {table} 表")
            continue
        for key, series in columns.items():
            size = len(series) if isinstance(series, list) else "?"
            if not isinstance(series, list) or size != length:
                problems.append(f"{label}: {table}.{key} 长度 {size} ≠ years（{length}）")
    return problems


# ── 总入口 ───────────────────────────────────────────────────────────────
def validate_products(data_dir: Path, *, schema_dir: Path | None = None) -> list[str]:
    """校验产物目录，返回问题列表（空列表 = 通过）。"""
    data_dir = Path(data_dir)
    schema_dir = Path(schema_dir) if schema_dir else config.SCHEMA_DIR
    problems: list[str] = []

    meta, errors = _read_json(data_dir / "meta.json")
    problems.extend(errors)
    if meta is None:
        return problems

    problems.extend(_check_schema(meta, schema_dir / "meta.schema.json", "meta.json"))
    listed: set[str] = set()

    ranking_dir = data_dir / "ranking"
    ranking_files = sorted(ranking_dir.glob("*.json")) if ranking_dir.is_dir() else []
    if not ranking_files:
        problems.append(f"缺少排行榜产物：{ranking_dir}/*.json")
    for path in ranking_files:
        data, errors = _read_json(path)
        problems.extend(errors)
        if data is None:
            continue
        label = f"ranking/{path.name}"
        problems.extend(_check_schema(data, schema_dir / "ranking.schema.json", label))
        problems.extend(_check_ranking(data, label, meta))
        listed.update(str(row.get("code")) for row in data.get("rows") or [])
        listed.update(str(row.get("code")) for row in data.get("excluded") or [])

    stock_dir = data_dir / "stock"
    stock_files = sorted(stock_dir.glob("*.json")) if stock_dir.is_dir() else []
    if not stock_files:
        problems.append(f"缺少个股产物：{stock_dir}/*.json")
    files_codes: set[str] = set()
    for path in stock_files:
        data, errors = _read_json(path)
        problems.extend(errors)
        if data is None:
            continue
        label = f"stock/{path.name}"
        problems.extend(_check_schema(data, schema_dir / "stock.schema.json", label))
        problems.extend(_check_scores(data, label))
        problems.extend(_check_tables(data, label))
        files_codes.add(path.stem)
        if str(data.get("code") or "") != path.stem:
            problems.append(f"{label}: 内嵌 code = {data.get('code')!r} 与文件名不一致")

    missing = sorted(listed - files_codes)
    if missing:
        problems.append(f"引用完整性：榜单有而 stock/ 缺文件：{missing}")
    orphans = sorted(files_codes - listed)
    if orphans:
        problems.append(f"引用完整性：stock/ 有而榜单未收录（孤儿文件）：{orphans}")

    index, errors = _read_json(data_dir / "search-index.json")
    problems.extend(errors)
    if index is not None:
        label = "search-index.json"
        problems.extend(_check_schema(index, schema_dir / "search-index.schema.json", label))
        problems.extend(_check_versions(index, label, meta))
        index_codes = {str(entry.get("code")) for entry in index.get("entries") or []}
        diff = sorted(index_codes ^ files_codes)
        if diff:
            problems.append(f"{label}: entries 与 stock/ 文件不一致：{diff}")

    industries, errors = _read_json(data_dir / "industries.json")
    problems.extend(errors)
    if industries is not None:
        problems.extend(_check_versions(industries, "industries.json", meta))
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PyramidSeek 产物校验（M3-5）")
    parser.add_argument("--data", type=Path, default=config.PUBLIC_DATA_DIR, help="产物目录")
    parser.add_argument("--schema", type=Path, default=config.SCHEMA_DIR, help="schema 目录")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    problems = validate_products(args.data, schema_dir=args.schema)
    if problems:
        for problem in problems:
            print(f"[validate][ERROR] {problem}", file=sys.stderr)
        print(f"[validate] 失败：{len(problems)} 个问题", file=sys.stderr)
        return 1
    print(f"[validate] 通过：{args.data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "parse_args", "validate_products"]
