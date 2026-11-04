"""CI 临时诊断：定位 tests/test_golden.py 基准对比失败的具体字段（用完即删）。

输出走 GitHub Actions annotation（`::error::`），公开仓库无需 admin 权限即可读。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.engine.scoring import score_universe

REPO = Path(__file__).resolve().parent
payload = json.loads((REPO / "fixtures" / "financials.json").read_text(encoding="utf-8"))
gold = json.loads((REPO / "fixtures" / "golden_python.json").read_text(encoding="utf-8"))

financials = {s["code"]: list(s["financials"].values()) for s in payload["stocks"]}
rows = [
    {
        "code": row.code,
        "compare_level": row.compare_level,
        "compare_group": row.compare_group,
        "quality": row.quality,
        "growth": row.growth,
        "growth_np": row.growth_np,
        "growth_ocf": row.growth_ocf,
        "score": row.score,
        "subs": dict(row.subs),
        "radar": dict(row.radar),
    }
    for row in score_universe(payload["stocks"], financials)
]

diffs: list[tuple[float, str, object, object]] = []


def walk(prefix: str, a: object, b: object) -> None:
    if isinstance(b, dict):
        for k in b:
            walk(f"{prefix}.{k}", (a or {}).get(k), b[k])
        return
    if a == b:
        return
    delta = float("inf")
    a_num = isinstance(a, int | float) and not isinstance(a, bool)
    b_num = isinstance(b, int | float) and not isinstance(b, bool)
    if a_num and b_num:
        delta = abs(float(a) - float(b))
    diffs.append((delta, prefix, a, b))


def emit(msg: str) -> None:
    print("::error::" + msg.replace("%", "%25").replace("\r", "")[:900])


if len(rows) != len(gold):
    emit(f"行数不同：py={len(rows)} golden={len(gold)}")
for x, y in zip(rows, gold, strict=False):
    walk(str(y.get("code", "?")), x, y)

diffs.sort(key=lambda t: -t[0])
if diffs:
    emit(f"字段差异 {len(diffs)} 处；最大绝对差 = {diffs[0][0]!r}")
else:
    emit("字段差异 0 处（逐字段值相等，差异只在结构/顺序）")
for delta, key, a, b in diffs[:10]:
    emit(f"{key}: py={a!r} golden={b!r} |delta|={delta!r}")
