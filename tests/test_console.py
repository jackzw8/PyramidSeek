"""跑批控制台冒烟测试（M5-4 / M5-5）。

用 Flask ``test_client`` 打真实 API：起一个 **offline 小样本** 跑批（不打网络、
fixtures 快），验证「状态可查 / 互斥 409 / 完成落库 / 日志可 tail」。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from console import utf8_env
from console.app import app
from console.runner import runner

RUN_TIMEOUT = 120  # offline 全量 fixtures 约 5~10s，留足余量


@pytest.fixture(autouse=True)
def _isolated_runs_db(tmp_path, monkeypatch):
    """每个用例用独立的 sqlite 历史库 + 独立的产物目录。

    不隔离的话有两处污染（都实测踩过）：
    - 历史库：测试里那次真实跑批会写进 `console/runs.db`（每跑一次 +3 条）；
    - **产物目录**：e2e 用例跑的是 `--offline --limit 2`（fixtures = 贵州茅台 + 五粮液），
      会覆盖真实 `public/data` —— 用户随后一点发布，就把这份假数据推上了 Pages。
    """
    monkeypatch.setenv("PS_CONSOLE_DB", str(tmp_path / "runs.db"))
    monkeypatch.setenv("PS_CONSOLE_OUT_DIR", str(tmp_path / "public_data"))


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


def test_child_process_env_forces_utf8() -> None:
    """子进程必须显式带 UTF-8 环境。

    Windows 上 `python -m src.batch` 的 stdout 是管道时按 locale（cp936）输出，
    控制台按 utf-8 解码 → 日志整片 `��`（U+FFFD，原字节已丢）。父进程开没开 UTF-8
    取决于启动它的终端，不能指望 —— 所以在子进程侧强制。
    """
    env = utf8_env()
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert "PATH" in env  # 继承父进程环境，不能只剩两个变量


def test_run_offline_end_to_end(client) -> None:
    """offline 小样本：启动 → 进度 → 完成 → 历史落库（M5-5 验收同款路径）。"""
    resp = client.post(
        "/api/run",
        # noStore：不写 DuckDB —— 否则会与用户正在跑的批抢锁，本用例因 exitCode=1 假失败
        json={"mode": "offline", "markets": ["A"], "limit": 2, "noStore": True},
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
    # 日志不得出现替换字符（编码不一致的直接症状）
    assert not any("\ufffd" in line for line in log), "日志含 U+FFFD：子进程输出与解码编码不一致"
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


def test_select_rejects_bad_level(client) -> None:
    """参数校验：层级只认 l1 / l2，且校验发生在联网之前（不必真拉数据）。"""
    resp = client.post("/api/select", json={"level": "l3"})
    assert resp.status_code == 400
    assert "level" in resp.get_json()["error"]


def test_select_rejects_dir_outside_repo(client, monkeypatch, tmp_path) -> None:
    """保存目录必须在仓库内（本机服务，不留任意路径写文件的口子）。"""
    monkeypatch.setattr("console.select.REPO_ROOT", tmp_path)
    resp = client.post("/api/select", json={"kind": "industry", "outDir": "../../.."})
    assert resp.status_code == 400
    assert "仓库内" in resp.get_json()["error"]


def test_select_writes_list(client, monkeypatch, tmp_path) -> None:
    """行业选样（mock 数据源）：每行业取前 N、跳过「未分类」与非 normal，清单落到 outDir。"""
    stocks = [
        {"code": "SH600000", "status": "normal", "l1_name": "银行", "l2_name": "银行"},
        {"code": "SH600001", "status": "normal", "l1_name": "银行", "l2_name": "银行"},
        {"code": "SZ000001", "status": "normal", "l1_name": "电子", "l2_name": "半导体"},
        {"code": "SZ000002", "status": "st", "l1_name": "电子", "l2_name": "半导体"},
        {"code": "SZ000003", "status": "normal", "l1_name": "", "l2_name": ""},
    ]

    class _FakeSource:
        def __init__(self, **_kwargs) -> None:
            pass

        def list_stocks(self, _market: str) -> list[dict]:
            return stocks

    monkeypatch.setattr("console.select.AkShareSource", _FakeSource)
    monkeypatch.setattr("console.select.REPO_ROOT", tmp_path)

    resp = client.post(
        "/api/select",
        json={"kind": "industry", "level": "l2", "perIndustry": 1, "outDir": "."},
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["count"] == 2  # 两个行业各取 1；未分类跳过；status=st 被过滤
    assert [g["name"] for g in body["groups"]] == ["半导体", "银行"]

    out = tmp_path / "industry_l2_top_codes.txt"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "SZ000001" in text and "SH600000" in text
    assert "SH600001" not in text  # 每行业只取 1 只
    assert "SZ000002" not in text  # 非 normal 被过滤
    assert "未分类" not in text


def test_select_hk_ggt_codes(client, monkeypatch, tmp_path) -> None:
    """港股通清单：代码归一成 HK + 5 位（700 → HK00700），去重有序。"""
    monkeypatch.setattr("console.select.REPO_ROOT", tmp_path)

    def _fake_frame() -> dict:
        return {"代码": ["01191", "700", "01191"]}

    class _Frame:
        def __getitem__(self, _key):
            return self

        def tolist(self):
            return _fake_frame()["代码"]

    monkeypatch.setattr("console.select.ak.stock_hk_ggt_components_em", lambda: _Frame())

    resp = client.post("/api/select", json={"kind": "hk_ggt", "outDir": "."})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["count"] == 2  # 01191 去重，700 补零
    out = tmp_path / "hk_ggt_codes.txt"
    text = out.read_text(encoding="utf-8")
    assert "HK01191" in text and "HK00700" in text


def test_fs_lists_dirs_and_txt_only(client, monkeypatch, tmp_path) -> None:
    """目录浏览：只给目录与 .txt，隐藏目录不出现；越界路径 400。"""
    (tmp_path / "sub").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("console.select.REPO_ROOT", tmp_path)
    monkeypatch.setattr("console.app.REPO_ROOT", tmp_path)

    body = client.get("/api/fs?path=").get_json()
    assert body["cwd"] == ""
    assert body["dirs"] == ["sub"]
    assert body["files"] == ["a.txt"]

    assert client.get("/api/fs?path=../../").status_code == 400


def test_shutdown_terminates_running_child(client) -> None:
    """退出钩子：控制台退出时收掉跑批子进程（它占着 DuckDB 与缓存目录）。

    不收的话，杀掉控制台后会留下孤儿 `src.batch`，下次启动直接报「任务独占」。
    """
    client.post("/api/run", json={"mode": "offline", "markets": ["A"], "limit": 2})

    proc = None
    for _ in range(50):
        proc = runner._proc  # noqa: SLF001 —— 测的正是「拿到子进程句柄」这件事
        if proc is not None and proc.poll() is None:
            break
        time.sleep(0.1)
    assert proc is not None and proc.poll() is None, "跑批子进程没起来"

    runner.shutdown()
    assert proc.poll() is not None, "shutdown 后子进程仍在运行"
    _wait_done(client)  # 让状态线程收尾，别影响后续用例
