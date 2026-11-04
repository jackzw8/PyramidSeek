"""跑批控制台（M5-4 / M5-5，2026-11-04）：Flask + 子进程跑批 + 任务互斥 + sqlite 历史。

    python -m console.app --port 5000     # 启动控制台，浏览器开 http://127.0.0.1:5000

- 子进程跑 `python -m src.batch ...`：**关掉浏览器跑批不中断**（进程挂在 Flask 下）；
- **任务互斥**（缓存与 DuckDB 独占）：同一时间只允许一个跑批，并发触发返回 409 并说明原因；
- 进度写 `data/_run/{run_id}/status.json` + 全量日志 `log.txt`（供 tail）；
- sqlite `console/runs.db` 记 run 元信息（M5-4）。
"""

from __future__ import annotations

import os

__all__ = ["utf8_env"]


def utf8_env() -> dict[str, str]:
    """子进程环境：强制 UTF-8 标准输出 / 标准错误。

    子进程的 stdout 一旦是**管道**（本控制台就是），Windows 上 Python 按 locale 编码
    输出（中文机器 = cp936），而父进程按 utf-8 解码 → 日志整片 ``��``（U+FFFD，
    原字节已丢、不可逆）。

    父进程自己开没开 UTF-8 模式取决于**启动它的终端**（cmd / PowerShell 默认不开，
    只有带 PYTHONUTF8=1 的 shell 才开），不能指望，所以在子进程侧强制。
    """
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
