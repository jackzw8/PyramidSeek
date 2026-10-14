"""清洗与标准化（占位，任务 M1-4）。

要点（M1-4 实现时）：
- 单位统一「亿元 / 小数」（家法 R10）；空值用 ``None``，**不用 0 代替**（家法 R11）；
- 勾稽校验 |资产 − (负债 + 权益)| / 资产 ≤ ``config.BALANCE_TOLERANCE``；
- 环比突变（±200%）标记待确认；异常有日志。
"""

from __future__ import annotations

from typing import Any


def clean_financials(code: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """标准化单只股票的财报（未实现）。"""
    raise NotImplementedError("clean_financials 待 M1-4 实现")


__all__ = ["clean_financials"]
