"""M2-6：Python 黄金样本回归（《评分算法规格》§11）。

- ``fixtures/golden_python.json`` 是 ``score_universe`` 在 ``fixtures/financials.json``
  上的**基准快照**（提交进库，防口径被无意改动）；
- 口径被无意改动即红；有意改参数则按 R3 升 ``formulaVersion`` 并更新快照。

历史：2026-11-11 前这里还有一条「Python 与 JS 原型逐项对账」用例；D-68 / ADR-017
起前端只渲染产物、评分只保留 Python 一个生产点，跨语言对账退役（见 08 ADR-017）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from src.engine.scoring import score_universe

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "financials.json"
GOLDEN = REPO / "fixtures" / "golden_python.json"

# 未取整字段的容差（06 §11 同口径）
TOL = 1e-9


def _python_rows() -> list[dict]:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    financials = {s["code"]: list(s["financials"].values()) for s in payload["stocks"]}
    return [
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


def _assert_close(actual: object, expected: object, path: str) -> None:
    """逐项比对：数值走 1e-9 容差，其余严格相等。

    radar / subs 里的 roe、roa、gross_margin、cash_ratio、net_margin 是**未取整**浮点，
    直接 ``==`` 会被平台浮点末位差异打成红：Windows+py3.14 与 Linux+py3.11 实测 15 处
    差 1e-14 量级（numpy 的 SIMD / 求逆路径不同）。口径真被改动时差异远大于 1e-9。
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: 期望 dict，实际 {type(actual).__name__}"
        assert set(actual) == set(expected), f"{path}: 键集合不同 {set(actual) ^ set(expected)}"
        for key in expected:
            _assert_close(actual[key], expected[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: 期望 list，实际 {type(actual).__name__}"
        assert len(actual) == len(expected), f"{path}: 长度 {len(actual)} != {len(expected)}"
        for index, (got, want) in enumerate(zip(actual, expected, strict=True)):
            _assert_close(got, want, f"{path}[{index}]")
        return
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"
        return
    if isinstance(expected, int | float):
        numeric = isinstance(actual, int | float) and not isinstance(actual, bool)
        assert numeric, f"{path}: 类型不同（{type(actual).__name__} vs {type(expected).__name__}）"
        diff = abs(float(actual) - float(expected))
        assert math.isclose(
            float(actual), float(expected), rel_tol=0.0, abs_tol=TOL
        ), f"{path}: {actual!r} != {expected!r}（差 {diff:g}）"
        return
    assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def test_python_matches_committed_golden() -> None:
    """Python 侧结果与**提交在库里的基准快照**逐项一致（口径被无意改动即红）。"""
    assert GOLDEN.exists(), "基准快照缺失：先生成 fixtures/golden_python.json"
    _assert_close(_python_rows(), json.loads(GOLDEN.read_text(encoding="utf-8")), "golden")
