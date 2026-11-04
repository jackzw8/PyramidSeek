"""跑批控制台 HTTP 层（M5-4）：Flask 路由 + 静态页。

    python -m console.app --port 5000

API：
- ``GET  /api/state``   当前任务状态 + 可选源 + 失败清单计数
- ``POST /api/run``     启动跑批（body：mode / markets / sourceA / sourceHk /
  codes / codesFile / limit / offline）
- ``POST /api/stop``    停止当前跑批
- ``GET  /api/log``     日志 tail（``?tail=200``）
- ``GET  /api/history`` sqlite 历史（最近 20 条）
- ``GET  /api/pending`` 失败清单（``data/_pending.json``）
- ``POST /api/publish`` 发布（``publish --push --yes``，同步执行，返回输出 tail）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

from src import config  # noqa: E402
from src.datasource.registry import (  # noqa: E402
    DEFAULT_SOURCE,
    available_sources,
    source_label,
)

from . import store, utf8_env  # noqa: E402
from .runner import BatchBusy, runner  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="")
PENDING_PATH = config.DATA_DIR / "_pending.json"


def _sources_payload() -> dict:
    return {
        market: [
            {
                "name": name,
                "label": source_label(market, name),
                "default": name == DEFAULT_SOURCE[market],
            }
            for name in available_sources(market)
        ]
        for market in config.MARKETS
    }


def _pending() -> list[dict]:
    if not PENDING_PATH.exists():
        return []
    try:
        return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return []


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def api_state():
    snapshot = runner.snapshot()
    return jsonify(
        {
            "running": snapshot.get("running", False),
            "status": snapshot,
            "sources": _sources_payload(),
            "pendingCount": len(_pending()),
            "history": store.list_runs(10),
            "publicDataDir": str(config.PUBLIC_DATA_DIR),
        }
    )


@app.post("/api/run")
def api_run():
    body = request.get_json(silent=True) or {}
    try:
        status = runner.start(
            mode=body.get("mode") or "daily",
            markets=body.get("markets"),
            source_a=body.get("sourceA") or None,
            source_hk=body.get("sourceHk") or None,
            codes=[c.strip() for c in (body.get("codes") or []) if c.strip()] or None,
            codes_file=body.get("codesFile") or None,
            limit=int(body["limit"]) if body.get("limit") else None,
            offline=bool(body.get("offline")),
        )
    except BatchBusy as exc:
        return jsonify({"error": str(exc)}), 409
    except (ValueError, KeyError) as exc:
        return jsonify({"error": f"参数无效：{exc}"}), 400
    return jsonify({"ok": True, "status": status})


@app.post("/api/stop")
def api_stop():
    return jsonify({"ok": runner.stop()})


@app.get("/api/log")
def api_log():
    tail = max(10, min(int(request.args.get("tail", 200)), 2000))
    return jsonify({"lines": runner.tail(tail)})


@app.get("/api/history")
def api_history():
    return jsonify({"runs": store.list_runs(20)})


@app.get("/api/pending")
def api_pending():
    return jsonify({"items": _pending()})


@app.post("/api/publish-gate")
def api_publish_gate():
    """只跑门禁不推送（``publish --check``），供「先跑门禁」按钮使用。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.build.publish", "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1200,
            check=False,
            env=utf8_env(),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "门禁超时（20 分钟）"}), 504
    output = (result.stdout or "") + (result.stderr or "")
    tail = [line for line in output.strip().splitlines() if line.strip()][-30:]
    return jsonify({"ok": result.returncode == 0, "exitCode": result.returncode, "output": tail})


@app.post("/api/publish")
def api_publish():
    if runner.snapshot().get("running"):
        return jsonify({"error": "跑批进行中，请先完成或停止后再发布"}), 409
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.build.publish", "--push", "--yes"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
            env=utf8_env(),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "发布超时（30 分钟）"}), 504
    output = (result.stdout or "") + (result.stderr or "")
    return jsonify(
        {
            "ok": result.returncode == 0,
            "exitCode": result.returncode,
            "output": output.strip().splitlines()[-40:],
        }
    ), (200 if result.returncode == 0 else 500)


def main() -> None:
    parser = argparse.ArgumentParser(description="PyramidSeek 跑批控制台（M5-4）")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"[console] http://{args.host}:{args.port}  （关掉浏览器跑批不中断）")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
