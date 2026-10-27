"""D-23：JS 评分引擎与标准层的**形状契约**（字段名 / 年报过滤 / 常量一致）。

数值级逐项对账属 M2-6（需要 node 环境）；这里先便宜地锁住「两版能吃同一份输入、
用同一套常量」，并防住 D-18 改名时引入的现役 bug（导出 `_dv: dv` 引用已不存在的 `dv`）。

演示前端的定位见 ``tests/conftest.py``（搬迁后不再假设两仓同级）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # PyramidSeekApp/


def test_engine_reads_standard_layer_field_names(demo_web_js_dir: Path) -> None:
    """`engine.js` 读标准层字段（`l1_name`/`l2_name`），不再有原型的 `sw_l1`/`sw_l2`。"""
    source = (demo_web_js_dir / "engine.js").read_text(encoding="utf-8")

    assert "sw_l1" not in source and "sw_l2" not in source
    assert "l1_name" in source and "l2_name" in source
    # 只吃年报（与 Python `annual_rows` 同构：过滤 → 按 report_period 排序 → 取最近 YEARS 条）
    assert "report_type === 'annual'" in source
    assert "report_period" in source
    # D-18 改名后遗留的现役 bug：导出 `_dv: dv` 引用不存在的 `dv` → 页面加载即崩
    assert "_dv" not in source
    assert "_safeDiv" in source


def test_engine_constants_match_config(demo_web_js_dir: Path) -> None:
    """权重常量与 `config.WEIGHTS` 逐字一致 —— M2-6 对账的第一道便宜防线。"""
    from src import config

    source = (demo_web_js_dir / "engine.js").read_text(encoding="utf-8")
    match = re.search(r"const W = \{([^}]*)\}", source)
    assert match, "找不到 JS 权重表"
    js_weights = {
        key: float(value) for key, value in re.findall(r"(\w+):\s*([\d.]+)", match.group(1))
    }
    assert js_weights == {key: float(value) for key, value in config.WEIGHTS.items()}
    assert re.search(r"FALLBACK_MIN = 30", source)  # §5 二级池回退阈值
    assert re.search(r"\bYEARS = 5\b", source)


def test_demo_data_matches_standard_layer_shape(demo_web_js_dir: Path) -> None:
    """演示数据 `data.js` 与标准层同名同构；财务行都标了年报（否则会被引擎全部滤掉）。"""
    source = (demo_web_js_dir / "data.js").read_text(encoding="utf-8")

    assert "sw_l1" not in source and "sw_l2" not in source
    assert '"report_type": "annual"' in source
    assert source.count('"report_type": "annual"') >= 50  # 11 只 × 5 年

    # 与标准层 fixtures 的字段名逐一对齐（这正是 D-23 的验收口径）
    fixture = json.loads((REPO / "fixtures" / "financials.json").read_text(encoding="utf-8"))
    stock = fixture["stocks"][0]
    assert {"l1_name", "l2_name", "market", "shares"} <= set(stock)
    for row in stock["financials"].values():
        assert {"revenue", "net_profit", "equity", "total_assets", "report_type"} <= set(row)
        break


def test_no_page_still_references_renamed_export(demo_web_js_dir: Path) -> None:
    """改名后旧导出 `_dv` 不应再被任何页面引用（改回 `PS._safeDiv`）。"""
    for path in demo_web_js_dir.glob("*.js"):
        assert "._dv" not in path.read_text(encoding="utf-8"), path.name
