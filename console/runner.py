"""跑批子进程管理（M5-4）：起停、任务互斥、进度状态文件、日志 tail。

- **互斥**：同一时间只允许一个跑批 —— 缓存目录与 DuckDB 都是独占资源，两个
  ``src.batch`` 并发会互相踩缓存（D-42 节流也会失效）。并发触发抛
  :class:`BatchBusy`，由 app 层翻译成 409 并说明原因；
- **关浏览器不中断**：子进程挂在 Flask 进程下，浏览器只是查看器；
- **进度**：后台线程逐行读子进程 stdout，正则抓「财报 [n/total]」「产物 X 个」
  等关键行 → ``data/_run/{run_id}/status.json``（每行更新）+ ``log.txt`` 全量。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src import config  # noqa: E402

from . import store, utf8_env  # noqa: E402

__all__ = ["BatchBusy", "Runner", "runner"]

_RUN_ROOT = config.DATA_DIR / "_run"
_RE_FIN = re.compile(r"财报 \[(\d+)/(\d+)\]")
_PHASE_MARKS = (
    ("采集完成", "build"),
    ("产物校验通过", "validate"),
    ("完成：", "done"),
)


class BatchBusy(RuntimeError):
    """已有跑批在运行（缓存与 DuckDB 独占）。"""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Runner:
    """单例：管理一个跑批子进程的生命周期。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._log_handle = None
        self.status: dict[str, Any] = {"running": False}

    # ── 启动 ─────────────────────────────────────────────────────────────
    def start(
        self,
        *,
        mode: str,
        markets: list[str] | None = None,
        source_a: str | None = None,
        source_hk: str | None = None,
        codes: list[str] | None = None,
        codes_file: str | None = None,
        limit: int | None = None,
        offline: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                running = self.status
                raise BatchBusy(
                    f"已有跑批在运行（run_id={running.get('runId')}，mode={running.get('mode')}）——"
                    "缓存目录与 DuckDB 为独占资源，请先停止或等待完成"
                )
            run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
            run_dir = _RUN_ROOT / run_id
            run_dir.mkdir(parents=True, exist_ok=True)

            # batch 的 offline 是独立开关（--offline），不是 --mode 取值
            if mode == "offline":
                mode, offline = "daily", True
            cmd = [sys.executable, "-m", "src.batch", "--mode", mode]
            if markets:
                cmd += ["--markets", *markets]
            if source_a:
                cmd += ["--source-a", source_a]
            if source_hk:
                cmd += ["--source-hk", source_hk]
            if codes:
                cmd += ["--codes", *codes]
            if codes_file:
                cmd += ["--codes-file", codes_file]
            if limit:
                cmd += ["--limit", str(limit)]
            if offline:
                cmd.append("--offline")
            # 产物目录可被覆盖：**测试必须指向 tmp** —— 否则 e2e 用例会把 fixtures 跑到
            # 真实 `public/data` 上，用户下次发布就发出「贵州茅台 + 五粮液」的假数据。
            out_override = os.environ.get("PS_CONSOLE_OUT_DIR")
            if out_override:
                cmd += ["--out", out_override]

            self._log_handle = (run_dir / "log.txt").open("w", encoding="utf-8")
            self._proc = subprocess.Popen(
                cmd,
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=utf8_env(),  # 子进程必须输出 UTF-8，否则按 cp936 写出 → 日志全 `��`
            )
            self.status = {
                "running": True,
                "runId": run_id,
                "startedAt": _now(),
                "finishedAt": None,
                "pid": self._proc.pid,
                "mode": mode,
                "markets": markets or list(config.MARKETS),
                "sources": {"A": source_a, "HK": source_hk},
                "cmd": " ".join(cmd),
                "phase": "collect",
                "progress": {"done": 0, "total": None},
                "errors": 0,
                "exitCode": None,
                "logPath": str(run_dir / "log.txt"),
            }
            self._write_status()
            store.record_run(
                run_id=run_id,
                started_at=self.status["startedAt"],
                mode=mode,
                markets=self.status["markets"],
                source_a=source_a,
                source_hk=source_hk,
                codes_note=(f"codes={len(codes)}" if codes else (codes_file or "")),
            )
            self._thread = threading.Thread(target=self._pump, args=(self._proc,), daemon=True)
            self._thread.start()
            return dict(self.status)

    # ── 后台泵：读 stdout → 解析进度 → 写状态 ────────────────────────────
    def _pump(self, proc: subprocess.Popen[str]) -> None:
        assert self._log_handle is not None
        for line in proc.stdout:  # type: ignore[union-attr]
            self._log_handle.write(line)
            self._log_handle.flush()
            self._parse(line)
        code = proc.wait()
        self._log_handle.close()
        self._log_handle = None
        with self._lock:
            self.status["running"] = False
            self.status["exitCode"] = code
            self.status["finishedAt"] = _now()
            self.status["phase"] = "done" if code == 0 else "failed"
            self._write_status()
            store.finish_run(
                self.status.get("runId", ""),
                self.status["phase"],
                code,
                f"产物目录见 public/data；errors={self.status['errors']}",
            )
            self._proc = None

    def _parse(self, line: str) -> None:
        status = self.status
        fin = _RE_FIN.search(line)
        if fin:
            status["phase"] = "collect"
            status["progress"] = {"done": int(fin.group(1)), "total": int(fin.group(2))}
        for marker, phase in _PHASE_MARKS:
            if marker in line:
                status["phase"] = phase
        if "ERROR" in line or "ERROR]" in line:
            status["errors"] = int(status.get("errors") or 0) + 1
        self._write_status()

    def _write_status(self) -> None:
        run_id = self.status.get("runId")
        if not run_id:
            return
        path = _RUN_ROOT / str(run_id) / "status.json"
        try:
            path.write_text(json.dumps(self.status, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # 状态文件是尽力而为，不影响跑批本身

    # ── 停止 / 查询 ──────────────────────────────────────────────────────
    def stop(self) -> bool:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return False
            self._proc.terminate()
            self.status["phase"] = "stopping"
            return True

    def shutdown(self) -> None:
        """退出兜底：收掉还在跑的跑批子进程。

        控制台被 Ctrl+C / 关窗口时，子进程**不会**自动退出（Windows 上父进程结束
        不连带杀子进程），而它占着 DuckDB 与缓存目录 —— 下次启动会莫名报「独占」。
        这里先 terminate，5 秒不退再 kill。
        """
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        print(f"[console] 退出中，正在结束跑批子进程 pid={proc.pid}", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def tail(self, lines: int = 200) -> list[str]:
        run_id = self.status.get("runId")
        if not run_id:
            return []
        path = _RUN_ROOT / str(run_id) / "log.txt"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.status)


runner = Runner()
