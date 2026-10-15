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
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.engine.scoring import score_universe

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "financials.json"
GOLDEN = REPO / "fixtures" / "golden_python.json"
ENGINE = REPO.parent / "项目文档" / "pyramidseek-web" / "js" / "engine.js"
RUNNER = REPO / "tests" / "run_engine.js"

# 子分 / 雷达里**不取整**的键（其余为分位子分，按 06 §11 已取整 → 严格相等）
UNROUNDED_KEYS = frozenset({"roe", "roa", "gross_margin", "cash_ratio", "net_margin"})
ROUNDED_FIELDS = ("quality", "growth", "growth_np", "growth_ocf", "score")


def _node() -> str | None:
    """定位 node：环境变量 → PATH → 本机便携版（2026-10-15 下载于 temp/）。"""
    env = os.environ.get("PYRAMIDSEEK_NODE")
    if env and Path(env).exists():
        return env
    which = shutil.which("node")
    if which:
        return which
    for candidate in sorted((REPO.parent / "temp" / "node").glob("*/node.exe")):
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


def test_python_matches_committed_golden() -> None:
    """Python 侧结果与**提交在库里的基准快照**逐项一致（口径被无意改动即红）。"""
    assert GOLDEN.exists(), "基准快照缺失：先生成 fixtures/golden_python.json"
    assert _python_rows() == json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.skipif(not ENGINE.exists(), reason="演示前端不在本仓库")
def test_js_engine_matches_python() -> None:
    """JS 参考实现吃同一份 fixtures，与 Python 逐项比对（§11 口径）。"""
    node = _node()
    if node is None:
        pytest.skip("找不到 node（设 PYRAMIDSEEK_NODE 或加入 PATH）")

    completed = subprocess.run(
        [node, str(RUNNER), str(FIXTURES), str(ENGINE)],
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
