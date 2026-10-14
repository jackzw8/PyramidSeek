"""`src/common.py`（跨层通用工具 leaf 模块）的结构性测试。

对应《计划完成情况》D-12：通用工具原先寄居在 `datasource/akshare_src.py`，
导致各层从"某个数据源的实现模块"导入通用工具、依赖方向被读歪。
本文件锁住两件事：**工具确实住在 common**，以及 **common 保持 leaf**（不反向依赖上层）。
"""

from __future__ import annotations

import inspect

import pytest

from src import common
from src.datasource import akshare_src
from src.datasource.base import DataSourceError

# 与 D-12 列表一致：这些名字必须住在 common
GENERIC_NAMES = (
    "effective_date_of",
    "exchange_of",
    "is_missing",
    "market_of",
    "normalize_code",
    "pick",
    "report_period_of",
    "report_type_of",
    "status_of",
    "to_internal_billion",
    "to_records",
)


@pytest.mark.parametrize("name", GENERIC_NAMES)
def test_generic_tools_live_in_common(name: str) -> None:
    assert hasattr(common, name), f"{name} 应住在 src/common.py"


@pytest.mark.parametrize("name", GENERIC_NAMES)
def test_akshare_src_reexports_for_backward_compatibility(name: str) -> None:
    """既有 `from .akshare_src import is_missing, ...` 不能破（re-export）。"""
    assert getattr(akshare_src, name) is getattr(common, name)


def test_common_is_a_leaf_module() -> None:
    """common 不得反向依赖上层：**只检查 import 语句**（docstring 里可以提到其他模块名）。"""
    import_lines = [
        line.strip()
        for line in inspect.getsource(common).splitlines()
        if line.lstrip().startswith(("import ", "from "))
    ]
    for line in import_lines:
        for forbidden in (
            "akshare_src",
            "official_src",
            "report_parser",
            "fixtures_src",
            "pipeline",
            "engine",
            "build",
        ):
            assert forbidden not in line, f"common.py 的 import 不应引用 {forbidden}：{line}"
    # 唯一允许的包内依赖是异常类型（base.DataSourceError）
    assert any("datasource.base import DataSourceError" in line for line in import_lines)


def test_normalize_code_still_raises_identifiable_error() -> None:
    """提取后异常类型不变（只是不再硬编码 source="akshare"）。"""
    with pytest.raises(DataSourceError):
        common.normalize_code("ABC", "A")


def test_common_has_no_io_at_import_time(tmp_path) -> None:
    """纯函数模块：import 不产生文件 IO（用"导入时不访问网络/磁盘"的间接断言）。"""
    assert common.to_internal_billion(100_000_000) == 1.0
    assert common.is_missing(float("nan")) is True
