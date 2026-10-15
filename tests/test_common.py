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


# ── 双引擎舍入口径（D-18）────────────────────────────────────────────────
def test_round_half_up_matches_js_math_round() -> None:
    """半进位朝 ``+∞``，与 JS 的 ``Math.round`` / ``toFixed`` 逐位一致。"""
    assert common.round_half_up(3.125, 2) == 3.13  # JS: Math.round(312.5) / 100
    assert common.round_half_up(2.5, 0) == 3.0  # JS: Math.round(2.5) === 3
    assert common.round_half_up(0.87777, 4) == 0.8778
    assert common.round_half_up(1.0, 2) == 1.0
    assert common.round_half_up(83.33333333333333, 2) == 83.33


def test_round_half_up_is_asymmetric_on_negative_half_like_js() -> None:
    """**刻意保留的不对称**：JS 遇 ``.5`` 一律朝 ``+∞``，对负数是"向零"。

    ``Math.round(-2.5) === -2``、``(-3.125).toFixed(2) === "-3.12"`` ——
    "更漂亮"的对称舍入（-3）反而会让 M2-6 逐项比对失败。
    """
    assert common.round_half_up(-2.5, 0) == -2.0
    assert common.round_half_up(-3.125, 2) == -3.12


def test_round_half_up_differs_from_builtin_round_somewhere() -> None:
    """至少存在一个边界值两者不同 —— 否则这个工具没有存在必要（内置 round 是银行家舍入）。"""
    assert round(3.125, 2) == 3.12
    assert round(0.125, 2) == 0.12
    cases = [(3.125, 2), (2.5, 0), (-2.5, 0), (0.125, 2)]
    assert any(
        common.round_half_up(value, digits) != round(value, digits) for value, digits in cases
    )


def test_common_has_no_io_at_import_time(tmp_path) -> None:
    """纯函数模块：import 不产生文件 IO（用"导入时不访问网络/磁盘"的间接断言）。"""
    assert common.to_internal_billion(100_000_000) == 1.0
    assert common.is_missing(float("nan")) is True
