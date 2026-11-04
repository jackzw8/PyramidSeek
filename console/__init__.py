"""跑批控制台（M5-4 / M5-5，2026-11-04）：Flask + 子进程跑批 + 任务互斥 + sqlite 历史。

    python -m console.app --port 5000     # 启动控制台，浏览器开 http://127.0.0.1:5000

- 子进程跑 `python -m src.batch ...`：**关掉浏览器跑批不中断**（进程挂在 Flask 下）；
- **任务互斥**（缓存与 DuckDB 独占）：同一时间只允许一个跑批，并发触发返回 409 并说明原因；
- 进度写 `data/_run/{run_id}/status.json` + 全量日志 `log.txt`（供 tail）；
- sqlite `console/runs.db` 记 run 元信息（M5-4）。
"""
