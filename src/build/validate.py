"""产物校验（占位，任务 M3-5）。

校验项（《产物契约》§6）：
- JSON Schema 合规（meta / ranking / stock）；
- 引用完整性：``stock/{code}`` 覆盖 ``ranking/{market}`` 全部行；
- 数值范围与版本字段存在。

校验失败必须**非零退出**，让 CI 拦住坏产物。
"""

from __future__ import annotations

from pathlib import Path


def validate_products(data_dir: Path) -> list[str]:
    """校验产物，返回问题列表（空列表 = 通过）（未实现）。"""
    raise NotImplementedError("validate_products 待 M3-5 实现")


__all__ = ["validate_products"]
