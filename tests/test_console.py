"""跑批控制台冒烟测试（M5-4 / M5-5）。

用 Flask ``test_client`` 打真实 API：起一个 **offline 小样本** 跑批（不打网络、
fixtures 快），验证「状态可查 / 互斥 409 / 完成落库 / 日志可 tail」。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from console.app import app

RUN_TIMEOUT = 120  # offline 全量 fixtures 约 5~10s，留足余量


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def _wait_done(client, timeout: float = RUN_TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get("/api/state").get_json()
        status = body["status"]
        if not status.get("running"):
            return status
        time.sleep(0.5)
    pytest.fail(f"跑批 {timeout}s 未结束")


def test_state_reports_sources_and_defaults(client) -> None:
    body = client.get("/api/state").get_json()
    assert body["running"] is False
    assert body["sources"]["A"][0]["name"] in {"sina", "em", "ths"}
    assert [s["name"] for s in body["sources"]["HK"]] == ["tx", "em"]
    assert any(s["default"] for s in body["sources"]["A"])


def test_run_offline_end_to_end(client) -> None:
    """offline 小样本：启动 → 进度 → 完成 → 历史落库（M5-5 验收同款路径）。"""
    resp = client.post(
        "/api/run",
        json={"mode": "offline", "markets": ["A"], "limit": 2},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"]["running"] is True

    status = _wait_done(client)
    assert status["exitCode"] == 0, status
    assert status["phase"] == "done"
    # 状态文件已写（M5-4：data/_run/{run_id}/status.json）
    assert Path(status["logPath"]).exists()
    # 日志可 tail 且包含产物行
    log = client.get("/api/log?tail=200").get_json()["lines"]
    assert any("产物" in line for line in log)
    # 历史落库
    history = client.get("/api/history").get_json()["runs"]
    assert any(run["run_id"] == status["runId"] and run["status"] == "done" for run in history)


def test_concurrent_run_is_rejected_with_reason(client) -> None:
    """任务互斥：跑批进行中触发第二个任务 → 409 + 原因（缓存与 DuckDB 独占）。"""
    client.post("/api/run", json={"mode": "offline", "markets": ["A"], "limit": 2})
    try:
        resp = client.post("/api/run", json={"mode": "offline", "markets": ["A"], "limit": 2})
        assert resp.status_code == 409
        assert "独占" in resp.get_json()["error"]
    finally:
        _wait_done(client)
