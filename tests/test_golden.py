"""M2-6：双引擎黄金样本对账（Python 与 JS 逐项比对，《评分算法规格》§11）。

- `fixtures/golden_python.json` 是 Python 侧 `score_universe` 在 `fixtures/financials.json`
  上的**基准快照**（提交进库，防口径被无意改动）；
- `tests/run_engine.js` 用 node 跑参考实现 `engine.js` 吃**同一份输入**，
  与 Python 逐项比对：Q / G / 两项单项成长分 / 总分 / 6 项子分 / 雷达六维 / 对比池。

比对口径（06 §11）：已取整字段**严格相等**；未取整字段容差 ``1e-9``
（JS ``Math.pow`` 与 Python ``**`` 末位可能差 1 ULP）。需要 node：
环境变量 ``PYRAMIDSEEK_NODE`` → ``PATH`` → 本机便携版；都找不到则跳过。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.engine.scoring import score_universe

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "financials.json"
GOLDEN = REPO / "fixtures" / "golden_python.json"
RUNNER = REPO / "tests" / "run_engine.js"

# 子分 / 雷达里**不取整**的键（其余为分位子分，按 06 §11 已取整 → 严格相等）
UNROUNDED_KEYS = frozenset({"roe", "roa", "gross_margin", "cash_ratio", "net_margin"})
ROUNDED_FIELDS = ("quality", "growth", "growth_np", "growth_ocf", "score")


def _node() -> str | None:
    """定位 node：环境变量 → PATH → 已知固定位置（2026-10-15 起在 adev-tools）。

    注意：注册表 PATH 变更后，**已启动的宿主进程**（IDE / 本工具的 shell 宿主）不会自动刷新，
    其子进程里 `shutil.which` 可能仍找不到 → 显式候选路径兜底。
    """
    env = os.environ.get("PYRAMIDSEEK_NODE")
    if env and Path(env).exists():
        return env
    which = shutil.which("node")
    if which:
        return which
    bases = (
        Path(r"D:\AIwork\adev-tools\node-v20.18.1-win-x64"),  # 用户固定位置（在用户 PATH）
        REPO.parent / "temp" / "node",  # 旧便携版位置（兼容保留）
    )
    for base in bases:
        for candidate in sorted(base.glob("node.exe")) or sorted(base.glob("*/node.exe")):
            return str(candidate)
    return None


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


def _pair_by_code(rows: list[dict]) -> dict[str, dict]:
    return {row["code"]: row for row in rows}


# 未取整字段的相对/绝对容差（06 §11 同口径）
TOL = 1e-9


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


def test_js_engine_matches_python(demo_web_js_dir: Path) -> None:
    """JS 参考实现吃同一份 fixtures，与 Python 逐项比对（§11 口径）。"""
    node = _node()
    if node is None:
        pytest.skip("找不到 node（设 PYRAMIDSEEK_NODE 或加入 PATH）")

    completed = subprocess.run(
        [node, str(RUNNER), str(FIXTURES), str(demo_web_js_dir / "engine.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, f"JS 引擎运行失败：{completed.stderr[-800:]}"
    js_rows = _pair_by_code(json.loads(completed.stdout))
    py_rows = _pair_by_code(_python_rows())

    assert set(js_rows) == set(py_rows)
    for code, js in js_rows.items():
        py = py_rows[code]
        assert js["compare_level"] == py["compare_level"], code
        assert js["compare_group"] == py["compare_group"], code
        for field in ROUNDED_FIELDS:  # 已取整 → 严格相等
            assert js[field] == py[field], f"{code}.{field}: js={js[field]} py={py[field]}"
        for table in ("subs", "radar"):
            assert set(js[table]) == set(py[table]), code
            for key, value in py[table].items():
                if key in UNROUNDED_KEYS:  # 未取整 → 容差 1e-9
                    assert js[table][key] == pytest.approx(value, abs=1e-9), f"{code}.{table}.{key}"
                else:  # 分位子分（取 2 位）→ 严格相等
                    assert js[table][key] == value, f"{code}.{table}.{key}"
