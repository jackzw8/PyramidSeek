"""发布层清单（M5-3 / ADR-014，2026-11-04）。

``manifest.json`` 不参与前端数据契约（``contractVersion`` 不因它变动），但**参与发布
门禁**：`publish` 用它保证「一次发布 = 一个不可分割的快照」。四条校验规则见
《产物契约》§3.7：文件存在且哈希相符 / 无孤儿 JSON / snapshotVersion 一致 / 任一不满足
即拒绝推送（M5-7）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .. import config
from . import write_json

__all__ = ["build_manifest", "verify_manifest", "stamp_published_at"]

_MANIFEST_NAME = "manifest.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _count_rows(path: Path) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if isinstance(payload, dict):
        return len(payload.get("rows") or [])
    return 0


def build_manifest(inputs: Any, out_dir: Path) -> Path:
    """扫描 ``out_dir``（= ``public/data/``）下全部 JSON，生成清单。

    ``files`` 覆盖除自身外的所有 JSON；``counts`` 取文件级数量（少一批一眼可见）。
    ``publishedAt`` 先置 ``None``，由 :func:`stamp_published_at` 在发布时补。
    """
    files: list[dict[str, Any]] = []
    total = 0
    stock_count = 0
    for path in sorted(out_dir.rglob("*.json")):
        if path.name == _MANIFEST_NAME:
            continue
        rel = path.relative_to(out_dir).as_posix()
        raw = path.read_bytes()
        files.append({"path": rel, "bytes": len(raw), "sha256": _sha256(raw)})
        total += len(raw)
        if rel.startswith("stock/"):
            stock_count += 1

    ranking_dir = out_dir / "ranking"
    industries = (
        json.loads((out_dir / "industries.json").read_text(encoding="utf-8"))
        if (out_dir / "industries.json").exists()
        else {}
    )
    counts = {
        "stocks": stock_count,
        "rankingA": _count_rows(ranking_dir / "A.json"),
        "rankingHK": _count_rows(ranking_dir / "HK.json"),
        "industries": len(industries) if isinstance(industries, dict) else 0,
    }
    payload = {
        "snapshotVersion": inputs.snapshot_version,
        "contractVersion": config.CONTRACT_VERSION,
        "formulaVersion": config.FORMULA_VERSION,
        "generatedAt": inputs.generated_at,
        "publishedAt": None,
        "files": files,
        "totalBytes": total,
        "counts": counts,
    }
    return write_json(out_dir / _MANIFEST_NAME, payload)


def verify_manifest(data_dir: Path) -> list[str]:
    """按《产物契约》§3.7 四条规则校验，返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    manifest_path = data_dir / _MANIFEST_NAME
    if not manifest_path.exists():
        return [f"缺少 {manifest_path}"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"manifest.json 解析失败：{exc}"]

    entries = {entry.get("path"): entry for entry in manifest.get("files") or []}
    for rel, entry in entries.items():
        path = data_dir / rel
        if not path.exists():
            problems.append(f"清单缺文件：{rel}")
            continue
        digest = _sha256(path.read_bytes())
        if digest != entry.get("sha256"):
            problems.append(
                f"哈希不符：{rel}（清单 {entry.get('sha256')[:12]}… / 实际 {digest[:12]}…）"
            )

    for path in sorted(data_dir.rglob("*.json")):
        rel = path.relative_to(data_dir).as_posix()
        if rel == _MANIFEST_NAME:
            continue
        if rel not in entries:
            problems.append(f"未登记的孤儿 JSON：{rel}（清单缺文件）")

    meta_path = data_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"meta.json 解析失败：{exc}")
        else:
            if meta.get("snapshotVersion") != manifest.get("snapshotVersion"):
                problems.append(
                    f"snapshotVersion 不一致：meta={meta.get('snapshotVersion')}"
                    f" manifest={manifest.get('snapshotVersion')}"
                )
    return problems


def stamp_published_at(data_dir: Path) -> str:
    """发布构建时补 ``publishedAt``（该字段不在 ``files`` 清单内，不影响哈希校验）。"""
    from datetime import UTC, datetime

    manifest_path = data_dir / _MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["publishedAt"] = stamp
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return stamp
