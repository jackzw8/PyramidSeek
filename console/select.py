"""选样清单生成（M5-6，2026-11-04）：行业选样 + 港股通成分。

两类清单（格式一致：每行一个代码，``#`` 开头为注释，跑批解析时忽略）：

- **行业选样**（A 股）：按申万一级 / 二级取每行业前 N 家（按代码升序，可复现）；
  数据来自 ``AkShareSource.list_stocks("A")`` 的行业字段，走 cold 缓存、命中秒回。
- **港股通**：东财港股通成分股（``ak.stock_hk_ggt_components_em``，约 670 只、13s），
  代码归一成 ``HK01191`` 形式。

输出目录可选（默认 ``data/lists``），但**限制在仓库根内** —— 这是个本地服务，
不给自己留"任意路径写文件"的口子。
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

import akshare as ak

from src import config
from src.datasource.akshare_src import AkShareSource

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = "data/lists"

UNCLASSIFIED = config.INDUSTRY_UNCLASSIFIED
LEVEL_LABEL = {"l1": "申万一级", "l2": "申万二级"}
LEVEL_FIELD = {"l1": "l1_name", "l2": "l2_name"}
INDUSTRY_FILE = {"l1": "industry_l1_top_codes.txt", "l2": "industry_l2_top_codes.txt"}
HK_GGT_FILE = "hk_ggt_codes.txt"

LIST_SUFFIX = ".txt"


def resolve_dir(out_dir: str | None) -> Path:
    """把目录解析成仓库内的绝对路径；越界（``../`` 或别的盘）直接拒绝。"""
    raw = (out_dir or DEFAULT_OUT_DIR).strip().replace("\\", "/")
    if not raw:
        raw = DEFAULT_OUT_DIR
    candidate = Path(raw)
    target = (candidate if candidate.is_absolute() else REPO_ROOT / candidate).resolve()
    if not target.is_relative_to(REPO_ROOT):
        raise ValueError(f"目录必须在仓库内：{raw}")
    return target


def _write_list(out_dir: str | None, name: str, header: str, lines: list[str]) -> Path:
    directory = resolve_dir(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    return path


def _payload(path: Path, count: int, groups: list[dict] | None = None) -> dict:
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:  # 测试会把输出目录指到仓库外
        rel = str(path)
    return {
        "path": rel,
        "absPath": str(path),
        "count": count,
        "groups": groups or [],
        "preview": path.read_text(encoding="utf-8").splitlines()[:10],
    }


def _group_name(row: dict, level: str) -> str:
    """取分组名（当前行业层级）；缺映射 / 空串统一归「未分类」。"""
    name = str(row.get(LEVEL_FIELD[level]) or "").strip()
    return name or UNCLASSIFIED


def select_industry(
    *,
    level: str = "l2",
    per_industry: int = 10,
    industries: list[str] | None = None,
    out_dir: str | None = None,
) -> dict:
    """按申万行业取每行业前 N 家 → 写清单。"""
    if level not in LEVEL_FIELD:
        raise ValueError(f"level 只能是 l1 / l2，收到 {level!r}")
    if not 1 <= per_industry <= 100:
        raise ValueError("每行业前 N 需在 1~100 之间")

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

    # 清单里按行业分组写注释（供人读），代码顺序与选样顺序一致
    stocks_by_code = {str(row["code"]): row for row in stocks}
    body: list[str] = []
    current = None
    for code in selected:
        row = stocks_by_code.get(code) or {}
        name = _group_name(row, level)
        if name != current:
            l1 = str(row.get("l1_name") or "").strip()
            head = name if level == "l1" or not l1 or l1 == name else f"{l1} / {name}"
            body.append(f"# ── {head}")
            current = name
        body.append(code)

    header = (
        f"# {LEVEL_LABEL[level]}行业前 {per_industry}（按代码序）"
        f"· 生成于 {time.strftime('%Y-%m-%d %H:%M')}"
    )
    path = _write_list(out_dir, INDUSTRY_FILE[level], header, body)
    return _payload(path, len(selected), groups)


def select_hk_ggt(*, out_dir: str | None = None) -> dict:
    """东财港股通成分股 → 港股清单（代码归一为 ``HK01191``）。"""
    frame = ak.stock_hk_ggt_components_em()
    codes = {"HK" + str(raw).strip().zfill(5) for raw in frame["代码"].tolist() if str(raw).strip()}
    ordered = sorted(codes)
    header = f"# 港股通成分股 {len(ordered)} 只（东财）· 生成于 {time.strftime('%Y-%m-%d %H:%M')}"
    path = _write_list(out_dir, HK_GGT_FILE, header, ordered)
    return _payload(path, len(ordered))


def select(kind: str = "industry", **kwargs) -> dict:
    """统一入口：``kind`` = ``industry``（默认）/ ``hk_ggt``。"""
    if kind == "hk_ggt":
        return select_hk_ggt(out_dir=kwargs.get("out_dir"))
    if kind != "industry":
        raise ValueError(f"kind 只能是 industry / hk_ggt，收到 {kind!r}")
    return select_industry(
        level=kwargs.get("level") or "l2",
        per_industry=int(kwargs.get("per_industry") or 10),
        industries=kwargs.get("industries") or None,
        out_dir=kwargs.get("out_dir"),
    )
