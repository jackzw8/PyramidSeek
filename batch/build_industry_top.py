"""冷启动跑批工具：按申万行业选样 + 后台启动 cold 跑批（D-32 决策 ④「本地分批」）。

两种用法：

1. **行业选样**（默认）：按申万行业（**默认二级**，`--level l1` 切回一级）取前 N 家生成清单，
   可 `--run` 直接启动跑批。
   数据源：``AkShareSource.list_stocks("A")``（申万一级/二级已回填；
   首跑建缓存约 1 分钟，之后秒回）。
   排序：按股票代码升序取每组前 N（可复现；近似「老牌主要公司」——若想要市值序，
   需等产物里有市值字段后再筛）。
   输出：``batch/industry_l2_top_codes.txt``（二级，默认）／``batch/industry_top_codes.txt``（一级）。
   每行一个代码，``#`` 注释带行业分组（二级清单的分组头写成「一级 / 二级」）。

2. **直接指定现成清单**（``--codes-file <路径>``）：跳过选样，直接后台启动 cold 跑批 ——
   （清单格式：每行一个代码，``#`` 为注释）。

用法（**在代码仓根 `PyramidSeekApp/` 下执行**；脚本自带路径推导，从别处调也行）：
    python batch/build_industry_top.py                       # 二级各前 10（默认，只出清单）
    python batch/build_industry_top.py --top-n 5             # 前 N 可传参（= --per-industry）
    python batch/build_industry_top.py --level l1            # 切回申万一级（旧口径，310 只）
    python batch/build_industry_top.py --run                 # 生成清单并后台启动（financials）
    python batch/build_industry_top.py --industries 半导体 光伏设备
                                                             # 只取指定行业（按当前层级名字）
    python batch/build_industry_top.py --codes-file <清单>    # 跳过选样，直接后台启动（续跑/扩池）
    python batch/build_industry_top.py --codes-file <清单> --foreground
                                                             # 前台运行，控制台实时看进度

跑批说明（**2026-10-16 更新，D-39 / D-40**）：
- 启动的模式是 **``--mode financials``**（三表 + 全市场快照，**不抓行情历史**）。
  原因：D-40 已停止月K 行情采集；``cold`` 模式会带 ``with_history=True`` 去抓行情
  （走东财且会限速），与 D-40 冲突 —— 建仓请一律用 ``financials``。
- 耗时：财报主源已换**同花顺 THS 三表**，≈**10s/只**（无限速）；310 只 ≈1 小时。
  二级口径（申万二级约 133 个 × 前 10，实际常不足 1300 只）耗时按只数线性放大。
- **中断后直接重跑同一条命令**，财报缓存固定落 ``data/raw/akshare/cold/``（D-37，与 partition
  无关），已抓的秒过；``financials`` 模式 TTL = 10 天，正常建仓不会过期。
**注意**：选样用的行业映射（``cold/list_A.json`` 与 ``sw_*.json``）无 TTL、永不过期；
想刷新（新上市/行业调整后重新选样）需先删这几个文件再跑本脚本。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

APP = Path(__file__).resolve().parent.parent  # 代码仓根 = PyramidSeekApp/
BATCH_DIR = Path(__file__).resolve().parent  # 本目录 = PyramidSeekApp/batch/
sys.path.insert(0, str(APP))

from src import config  # noqa: E402
from src.datasource.akshare_src import AkShareSource  # noqa: E402

SECONDS_PER_STOCK = 13  # 2026-10-24 实测：新浪主源三表 ≈4s/张 + 2~3s 节流 ≈13s/只（IPv6 修好后）
# D-39 记为 10s/只，是 THS 口径；D-45 换新浪主源后按实测调正

UNCLASSIFIED = config.INDUSTRY_UNCLASSIFIED  # 「未分类」不参与行业分位，选样时跳过
LEVEL_LABEL = {"l1": "申万一级", "l2": "申万二级"}
LEVEL_FIELD = {"l1": "l1_name", "l2": "l2_name"}


def log(text: str = "") -> None:
    print(text, flush=True)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    """按**显示宽度**补空格（中文/全角/「Ⅱ」这类东亚歧义宽字符算 2 格）。

    二级行业名长度差异大（``IT服务Ⅱ`` vs ``专业连锁Ⅱ``），不对齐会串行。
    """
    wide = sum(2 if unicodedata.east_asian_width(ch) in "WFA" else 1 for ch in text)
    gap = " " * max(0, width - wide)
    return gap + text if right else text + gap


def _out_file(level: str) -> Path:
    """选样清单路径：一级沿用旧名（文档/命令行无需改），二级加 ``_l2`` 区分，互不覆盖。"""
    suffix = "" if level == "l1" else f"_{level}"
    return BATCH_DIR / f"industry{suffix}_top_codes.txt"


def _child_env() -> dict[str, str]:
    """子进程环境：控制台/重定向中文不乱码。

    IPv6 自检由子进程自己完成（``src.batch`` → ``src.net.ensure_ipv4_preferred``，D-46），
    这里不再注入任何补丁：直连 ``python -m src.batch`` 与经本脚本启动走同一条路径。
    """
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _launch_cold(
    codes_file: Path, *, out_log: Path, err_log: Path, foreground: bool = False
) -> None:
    """启动跑批（``--codes-file`` + 动态 generated-at）。

    模式 = ``financials``（D-40）：三表 + 全市场快照，**不抓行情历史**。
    不用 ``cold`` —— 它会带 ``with_history=True`` 抓行情（东财、会限速），与 D-40 冲突。

    :param foreground: ``True`` = 前台运行，输出直连当前控制台（Ctrl+C 中断，
        重跑同一条命令即续传）；``False`` = 后台进程，日志追加到文件。
    """
    cmd = [
        sys.executable,
        "-m",
        "src.batch",
        "--mode",
        "financials",
        "--codes-file",
        str(codes_file),
        "--generated-at",
        time.strftime("%Y-%m-%dT00:00:00Z"),
    ]
    env = _child_env()  # 中文不乱码；IPv6 自检由子进程自己做（D-46）
    if foreground:
        log("\n前台运行跑批（financials 模式；Ctrl+C 中断，重跑同一条命令即续传）…\n")
        subprocess.run(cmd, cwd=str(APP), env=env, check=False)  # noqa: S603
        return
    log(f"\n启动 financials 模式跑批（三表 + 快照，不抓行情；后台，日志 {out_log.as_posix()}）…")
    subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(APP),
        env=env,
        stdout=open(out_log, "ab"),  # noqa: SIM115 —— 后台进程日志落盘，追加跨批次
        stderr=open(err_log, "ab"),
    )
    log(f"已启动。进度：Get-Content {out_log.as_posix()} -Wait -Tail 20")
    progress = Path(__file__).as_posix().replace("build_industry_top.py", "check_cold_progress.py")
    log(f"缓存计数：python {progress}")


def _group_name(row: dict, level: str) -> str:
    """取分组名（当前行业层级）；缺映射/空串统一归「未分类」。"""
    name = str(row.get(LEVEL_FIELD[level]) or "").strip()
    return name or UNCLASSIFIED


def _select_by_industry(
    args: argparse.Namespace,
) -> tuple[list[str], list[tuple[str, int, int]], dict[str, dict]]:
    """按申万行业（``--level`` 指定层级）选前 N 家（跳过「未分类」）。

    :return: （选中代码，``[(分组名, 池内数, 入选数), ...]``，代码信息表）
    """
    level = args.level
    # 选样也用 cold 固定分区：list_A/sw_* 与冷启动跑批共享同一份缓存（D-34），不重抓
    # （这些映射键无 TTL；刷新见 docstring「注意」）
    source = AkShareSource(partition="cold")
    log(f"拉取 A 股列表（{LEVEL_LABEL[level]}已回填；缓存命中秒回）…")
    stocks = source.list_stocks("A")
    log(f"共 {len(stocks)} 只（含非 normal，已过滤）")

    by_group: dict[str, list[dict]] = defaultdict(list)
    for row in stocks:
        if row.get("status") != "normal":
            continue
        by_group[_group_name(row, level)].append(row)

    selected: list[str] = []
    groups: list[tuple[str, int, int]] = []
    for name in sorted(by_group):
        if name == UNCLASSIFIED:
            continue  # 未分类不参与行业分位（产物单列），抓了浪费时长
        if args.industries and name not in args.industries:
            continue
        rows = sorted(by_group[name], key=lambda item: str(item["code"]))
        top = [str(item["code"]) for item in rows[: args.per_industry]]
        groups.append((name, len(rows), len(top)))
        selected.extend(top)
    return selected, groups, {str(row["code"]): row for row in stocks}


def _write_list(
    selected: list[str],
    stocks_by_code: dict[str, dict],
    per_industry: int,
    level: str,
) -> Path:
    """写选样清单（``#`` 行带行业分组，供人读；跑批解析时忽略）。"""
    out_file = _out_file(level)
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
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按申万行业（默认二级）生成跑批清单 / 直接启动跑批"
        "（financials 模式：三表 + 快照，不抓行情）"
    )
    parser.add_argument(
        "--level",
        choices=sorted(LEVEL_FIELD),
        default="l2",
        help="行业层级：l2 = 申万二级（默认），l1 = 申万一级",
    )
    parser.add_argument(
        "--per-industry",
        "--top-n",
        dest="per_industry",
        type=int,
        default=10,
        help="每个行业取前 N 家（默认 10；两个写法等价）",
    )
    parser.add_argument(
        "--industries",
        nargs="*",
        default=None,
        help="只取这些行业（按 --level 对应层级的名字，默认全部）",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="生成清单后直接后台启动跑批（financials 模式）",
    )
    parser.add_argument(
        "--codes-file",
        type=Path,
        default=None,
        help="跳过选样，直接用现成清单启动 cold 跑批（清单：每行一个代码，# 为注释）",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="前台运行（控制台实时看输出）；默认后台 + 日志落盘。配合 --run 或 --codes-file 使用",
    )
    args = parser.parse_args()

    # ── 模式 2：直接指定现成清单 ─────────────────────────────────────────
    if args.codes_file:
        # 相对路径按「调用时 CWD」定位；子进程 cwd=APP，必须先转绝对路径再传（实跑踩过）
        codes_file = args.codes_file.resolve()
        if not codes_file.exists():
            log(f"清单不存在：{codes_file}")
            return 1
        log(f"跳过选样，直接启动 cold 跑批：{codes_file}")
        _launch_cold(
            codes_file,
            out_log=codes_file.parent / f"{codes_file.stem}_out.txt",
            err_log=codes_file.parent / f"{codes_file.stem}_err.txt",
            foreground=args.foreground,
        )
        return 0

    # ── 模式 1：行业选样 ────────────────────────────────────────────────
    if args.per_industry < 1:
        log("--per-industry 至少为 1")
        return 1
    selected, groups, stocks_by_code = _select_by_industry(args)
    if not selected:
        # 常见原因：--industries 传了另一层级的名字（如默认二级下传「银行」）→ 别写空清单
        log(
            f"没选到任何标的（层级 {args.level}，--industries={args.industries}）。"
            f"检查行业名是否属于{LEVEL_LABEL[args.level]}；清单未改动。"
        )
        return 1
    out_file = _write_list(selected, stocks_by_code, args.per_industry, args.level)

    label = LEVEL_LABEL[args.level]
    log(f"\n{_pad('行业', 20)}{_pad('池内', 8, right=True)}{_pad('入选', 8, right=True)}")
    for name, total, picked in groups:
        log(f"{_pad(name, 20)}{_pad(str(total), 8, right=True)}{_pad(str(picked), 8, right=True)}")
    hours = len(selected) * SECONDS_PER_STOCK / 3600
    log(f"\n合计 {len(groups)} 个{label}行业 / 入选 {len(selected)} 只 → {out_file}")
    log(
        f"预计串行 ≈ {hours:.1f} 小时（{SECONDS_PER_STOCK}s/只：新浪主源三表 + 节流，不含行情）；"
        f"中断后重跑同一条命令，已抓的走缓存秒过（cold 固定分区，跨天续传）"
    )

    if args.run:
        stem = f"industry_{args.level}" if args.level != "l1" else "industry"
        _launch_cold(
            out_file,
            out_log=out_file.parent / f"{stem}_cold_out.txt",
            err_log=out_file.parent / f"{stem}_cold_err.txt",
            foreground=args.foreground,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
