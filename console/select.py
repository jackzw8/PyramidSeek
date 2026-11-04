"""行业选样（M5-6，2026-11-04）：按申万行业取前 N 家 → 生成跑批清单。

从 `batch/build_industry_top.py` 搬进来：那个脚本的另一半（选样后直接启动跑批）
已被控制台取代，只剩「生成清单」还需要 —— 控制台一次跑批只要一个 ``--codes-file``。

清单格式与旧脚本**完全一致**（每行一个代码，``#`` 为注释、分组头写「一级 / 二级」），
所以以前生成的清单可直接继续用；输出位置也不变（``batch/*_codes.txt``，已 gitignore）。

选样数据：``AkShareSource.list_stocks("A")`` 的申万一级 / 二级字段。
按代码升序取每组前 N（可复现；近似「老牌主要公司」，不是市值序）。
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

from src import config
from src.datasource.akshare_src import AkShareSource

REPO_ROOT = Path(__file__).resolve().parents[1]
BATCH_DIR = REPO_ROOT / "batch"

UNCLASSIFIED = config.INDUSTRY_UNCLASSIFIED
LEVEL_LABEL = {"l1": "申万一级", "l2": "申万二级"}
LEVEL_FIELD = {"l1": "l1_name", "l2": "l2_name"}
OUT_NAME = {"l1": "industry_top_codes.txt", "l2": "industry_l2_top_codes.txt"}


def _group_name(row: dict, level: str) -> str:
    """取分组名（当前行业层级）；缺映射 / 空串统一归「未分类」。"""
    name = str(row.get(LEVEL_FIELD[level]) or "").strip()
    return name or UNCLASSIFIED


def _write_list(
    selected: list[str],
    stocks_by_code: dict[str, dict],
    per_industry: int,
    level: str,
) -> Path:
    """写选样清单（``#`` 行带行业分组，供人读；跑批解析时忽略）。"""
    path = BATCH_DIR / OUT_NAME[level]
    lines = [
        f"# {LEVEL_LABEL[level]}行业前 {per_industry}（按代码序）"
        f"· 生成于 {time.strftime('%Y-%m-%d %H:%M')}"
    ]
    current = None
    for code in selected:
        row = stocks_by_code.get(code) or {}
        name = _group_name(row, level)
        if name != current:
            l1 = str(row.get("l1_name") or "").strip()
            head = name if level == "l1" or not l1 or l1 == name else f"{l1} / {name}"
            lines.append(f"# ── {head}")
            current = name
        lines.append(code)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def select_by_industry(
    *,
    level: str = "l2",
    per_industry: int = 10,
    industries: list[str] | None = None,
) -> dict:
    """按行业选样并写清单。

    :returns: ``{path, absPath, count, groups, preview}``；
        ``path`` 是相对仓库根的 posix 路径，可直接填进 ``--codes-file``。
    :raises ValueError: 参数不合法
    """
    if level not in LEVEL_FIELD:
        raise ValueError(f"level 只能是 l1 / l2，收到 {level!r}")
    if not 1 <= per_industry <= 100:
        raise ValueError("每行业前 N 需在 1~100 之间")

    # 选样与跑批共享 cold 缓存（D-34）：行业映射无 TTL，命中秒回；首次约 1 分钟
    stocks = AkShareSource(partition="cold").list_stocks("A")

    by_group: dict[str, list[dict]] = defaultdict(list)
    for row in stocks:
        if row.get("status") != "normal":
            continue
        by_group[_group_name(row, level)].append(row)

    wanted = set(industries) if industries else None
    selected: list[str] = []
    groups: list[dict] = []
    for name in sorted(by_group):
        if name == UNCLASSIFIED:  # 未分类不参与行业分位，抓了浪费时长
            continue
        if wanted is not None and name not in wanted:
            continue
        rows = sorted(by_group[name], key=lambda item: str(item["code"]))
        top = [str(item["code"]) for item in rows[:per_industry]]
        groups.append({"name": name, "pool": len(rows), "picked": len(top)})
        selected.extend(top)

    path = _write_list(selected, {str(row["code"]): row for row in stocks}, per_industry, level)
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:  # 测试会把 BATCH_DIR 指到仓库外
        rel = str(path)
    return {
        "path": rel,
        "absPath": str(path),
        "count": len(selected),
        "groups": groups,
        "preview": path.read_text(encoding="utf-8").splitlines()[:10],
    }
