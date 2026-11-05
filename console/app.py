"""跑批控制台 HTTP 层（M5-4）：Flask 路由 + 静态页。

    python -m console.app --port 5000

API：
- ``GET  /api/state``   当前任务状态 + 可选源 + 失败清单计数
- ``POST /api/run``     启动跑批（body：mode / markets / sourceA / sourceHk /
  codes / codesFile / limit / offline）
- ``POST /api/stop``    停止当前跑批
- ``POST /api/select``  生成选样清单（行业选样 / 港股通成分股，目录可选）
- ``GET  /api/fs``      目录浏览（限仓库内，供前端挑目录与清单文件）
- ``GET  /api/log``     日志 tail（``?tail=200``）
- ``GET  /api/history`` sqlite 历史（最近 20 条）
- ``GET  /api/pending`` 失败清单（``data/_pending.json``）
- ``POST /api/publish`` 发布（``publish --push --yes``，同步执行，返回输出 tail）
"""

from __future__ import annotations

import argparse
import atexit
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
from .select import LIST_SUFFIX, REPO_ROOT, resolve_dir, select  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="")
PENDING_PATH = config.DATA_DIR / "_pending.json"

# 进程退出（Ctrl+C / 关闭控制台窗口）时收掉跑批子进程 —— 它占着 DuckDB 与缓存目录，
# 不收拾会导致「明明没任务在跑，却报独占」。强杀（任务管理器结束进程）时 atexit 不执行，
# 那是操作系统层面的限制，只能靠下次启动时人工确认。
atexit.register(runner.shutdown)


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
            merge=bool(body.get("merge")),
            no_store=bool(body.get("noStore")),
        )
    except BatchBusy as exc:
        return jsonify({"error": str(exc)}), 409
    except (ValueError, KeyError) as exc:
        return jsonify({"error": f"参数无效：{exc}"}), 400
    return jsonify({"ok": True, "status": status})


@app.post("/api/stop")
def api_stop():
    return jsonify({"ok": runner.stop()})


@app.post("/api/select")
def api_select():
    """生成选样清单（供 ``--codes-file``）。

    - ``kind=industry``：按申万一级 / 二级取每行业前 N（A 股），读 cold 缓存、命中秒回；
    - ``kind=hk_ggt``：东财港股通成分股（约 670 只，约 13s）；
    - ``outDir``：保存目录（相对仓库根，默认 ``data/lists``；越界拒绝）。
    """
    body = request.get_json(silent=True) or {}
    try:
        result = select(
            kind=str(body.get("kind") or "industry"),
            level=str(body.get("level") or "l2"),
            per_industry=int(body.get("perIndustry") or 10),
            industries=body.get("industries") or None,
            out_dir=body.get("outDir") or None,
        )
    except ValueError as exc:
        return jsonify({"error": f"参数无效：{exc}"}), 400
    except Exception as exc:  # 选样要联网，把上游异常翻成可读信息
        return jsonify({"error": f"生成失败：{type(exc).__name__}: {exc}"}), 502
    return jsonify({"ok": True, **result})


@app.get("/api/fs")
def api_fs():
    """目录浏览（**仅限仓库内**）：给前端挑「保存目录」与「清单文件」。

    浏览器拿不到本地路径（安全限制），所以由本机后端代列目录 —— 只返回目录名
    与 ``*.txt``，拒绝 ``../`` 逃逸与隐藏目录。
    """
    raw = request.args.get("path") or ""
    try:
        target = resolve_dir(raw or ".")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not target.is_dir():
        target = REPO_ROOT

    dirs: list[str] = []
    files: list[str] = []
    for item in sorted(target.iterdir(), key=lambda p: p.name):
        if item.name.startswith(".") or item.name == "__pycache__":
            continue
        if item.is_dir():
            dirs.append(item.name)
        elif item.suffix.lower() == LIST_SUFFIX:
            files.append(item.name)

    rel = target.relative_to(REPO_ROOT).as_posix()
    parent = None
    if target != REPO_ROOT:
        parent = target.parent.relative_to(REPO_ROOT).as_posix() or "."
    return jsonify(
        {"cwd": "" if rel == "." else rel, "parent": parent, "dirs": dirs, "files": files}
    )


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


@app.get("/api/product")
def api_product():
    """当前待发布产物的摘要（发布前二次确认用）。

    动机：发布只校验「产物合不合规」，不校验「这是不是你想发的那份」。曾出现
    e2e 测试把 fixtures 跑到 `public/data` 后用户一点发布就推上 Pages —— 有摘要
    就能一眼看出「怎么是五粮液，不是我跑的腾讯」。
    """
    data_dir = config.PUBLIC_DATA_DIR
    meta_path = data_dir / "meta.json"
    if not meta_path.exists():
        return jsonify({"error": "assets/public/data 里还没有产物，请先跑一次批"}), 404

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    rows: dict[str, int] = {}
    for market in config.MARKETS:
        path = data_dir / "ranking" / f"{market}.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows[market] = len(payload.get("rows") or [])
    codes = sorted(item.stem for item in (data_dir / "stock").glob("*.json"))
    return jsonify(
        {
            "generatedAt": meta.get("generatedAt"),
            "snapshotVersion": meta.get("snapshotVersion"),
            "byMarket": (meta.get("universe") or {}).get("byMarket") or {},
            "rows": rows,
            "codes": codes[:8],
            "codeCount": len(codes),
        }
    )


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
    # 这一行走 Python 自身的 stdout，可能被重定向/管道按不同编码解释；
    # 用纯 ASCII，避免与窗口代码页（bat 里 chcp 936）打架。中文提示在 bat 里给。
    print(f"[console] http://{args.host}:{args.port}  (closing browser keeps batch)")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
