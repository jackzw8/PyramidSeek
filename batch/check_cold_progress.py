"""跑批进度：按固定分区 cold/ 的缓存文件数估算已完成标的数（D-37）。

D-37 后 monthly/financials 重抓也写 cold/（覆盖同键文件），故文件数只增不减、
计数口径不变；mtime 仅用于跑批时判新鲜，与本脚本的计数无关。

**2026-10-16（D-40）**：行情历史已停止采集 → 行情缓存数**不再增长**，本脚本的行情行
仅作历史参考（显示已抓到的旧行情），**不作为完成度指标**；看进度请只看「三表齐备标的」。

**键名（D-44 起带源名）**：``financial_{源}_{code}_{slug}.json``（如
``financial_sina_SH600000_income.json`` / ``financial_ths_SH600000_income.json``）——
按 ``code`` 聚合、看是否三张（balance/income/cashflow）齐备，与走哪个源无关。
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

APP = Path(__file__).resolve().parent.parent  # 代码仓根 = PyramidSeekApp/
BATCH_DIR = Path(__file__).resolve().parent  # 本目录 = PyramidSeekApp/batch/
sys.path.insert(0, str(APP))

from src import config  # noqa: E402

COLD_DIR = config.DATA_DIR / "raw" / "akshare" / "cold"  # 与数据源共用同一份路径定义
DEFAULT_TARGET = 310  # 旧一级口径（31 行业 × 前 10）
REPORTS = {"balance", "income", "cashflow"}
FIN_RE = re.compile(
    r"^financial_(?P<src>[a-z0-9]+)_(?P<code>[A-Z]{2}\d{6})_(?P<slug>[a-z]+)\.json$"
)


def _target(level_file: str) -> int:
    """目标只数 = 清单里非 ``#`` 行数（一级 310 / 二级 1222 自动适应）。"""
    for name in ([level_file] if level_file else []) + [
        "industry_l2_top_codes.txt",
        "industry_top_codes.txt",
    ]:
        path = BATCH_DIR / name
        if path.exists():
            codes = [
                line for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            return len(codes) or DEFAULT_TARGET
    return DEFAULT_TARGET


TARGET = _target(sys.argv[1] if len(sys.argv) > 1 else "")

fin = list(COLD_DIR.glob("financial_*.json"))
price_raw = list(COLD_DIR.glob("price_*_raw.json"))
price_qfq = list(COLD_DIR.glob("price_*_qfq.json"))
print(f"目标清单: {TARGET} 只（清单文件可在命令行指定，默认取 batch/ 下的二级清单）")
print(f"财报缓存: {len(fin)} 张（目标 {TARGET}×3={TARGET * 3}）")
print(f"行情缓存: raw {len(price_raw)} / qfq {len(price_qfq)}（D-40 起停止采集，仅供历史参考）")
by_code: dict[str, set[str]] = defaultdict(set)
for path in fin:
    match = FIN_RE.match(path.name)
    if match:
        by_code[match["code"]].add(match["slug"])
done = sum(1 for slugs in by_code.values() if REPORTS <= slugs)
print(f"三表齐备标的: {done} / {TARGET}")
