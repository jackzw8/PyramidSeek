"""``BuildInputs`` 索引缓存与产物写出（``write_json``）的契约测试。

对应审核项：**PL-06**（索引每次重建 → 全市场 O(n²)）、**PL-19**（非原子写 + 放行 NaN）。
"""

from __future__ import annotations

import json

import pytest

from src.build import BuildInputs, write_json


def _inputs(n: int = 3) -> BuildInputs:
    stocks = tuple({"code": f"SH60000{i}"} for i in range(n))
    return BuildInputs(snapshot_version="v1", generated_at="2026-10-27T00:00:00Z", stocks=stocks)


def test_code_index_is_cached_not_rebuilt() -> None:
    """PL-06：``build_stock`` 每只股票访问两次索引，每次重建 → 5000 只约 1.5 万次全量 dict。"""
    inputs = _inputs()
    assert inputs.stocks_by_code is inputs.stocks_by_code  # cached_property 返回同一对象


def test_code_index_content() -> None:
    assert set(_inputs().stocks_by_code) == {"SH600000", "SH600001", "SH600002"}
    assert _inputs().scores_by_code == {}


def test_write_json_rejects_nan(tmp_path) -> None:
    """PL-19：``NaN`` 会被写成裸字面量 —— Python 侧校验能过，浏览器 ``JSON.parse`` 直接崩。"""
    with pytest.raises(ValueError):
        write_json(tmp_path / "bad.json", {"score": float("nan")})

    assert not (tmp_path / "bad.json").exists()  # 不留下半截（非法）产物
    assert not (tmp_path / "bad.json.tmp").exists()  # 临时文件已清理


def test_write_json_keeps_previous_version_when_failing(tmp_path) -> None:
    """原子写：失败时旧产物必须完好（原先先 open 截断再 dump → 旧产物已被毁）。"""
    target = tmp_path / "meta.json"
    write_json(target, {"v": 1})

    with pytest.raises(ValueError):
        write_json(target, {"v": float("inf")})

    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}


def test_write_json_shape(tmp_path) -> None:
    target = write_json(tmp_path / "sub" / "ok.json", {"中文": 1})  # 自动建父目录
    text = target.read_text(encoding="utf-8")
    assert json.loads(text) == {"中文": 1}
    assert text.endswith("\n")
