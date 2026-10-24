# 跑批脚本使用说明（batch/）

> 本目录 = 冷启动/大样本跑批的**辅助脚本与清单**（**已在代码仓内**：`PyramidSeekApp/batch/`，2026-10-24 从项目根移入）。
> 正式跑批入口仍是 `python -m src.batch`，本目录只是它的「驾驶辅助」：选样、清单、后台启动、进度查看。
> 将来 M6-5 的本地后台管理界面会取代这里的部分功能（D-36，先记录未编码）。
> **入库范围**：脚本与本文入库；`*_codes.txt`（选样清单）与 `*_out.txt` / `*_err.txt`（跑批日志）由
> `.gitignore` 排除 —— 它们每次跑批都会重写，留在本地即可（清单可随时重新生成，确定性来自代码序）。

## 目录内容

| 文件 | 用途 |
|---|---|
| `build_industry_top.py` | 冷启动跑批工具：① 行业选样（**默认申万二级**，每行业前 N 由 `--per-industry` / `--top-n` 传入，`--level l1` 切一级，`--run` 启动跑批）；② `--codes-file <清单>` 跳过选样、直接启动 cold 跑批（续跑/扩池/任意清单）；`--foreground` 前台实时看输出 |
| `check_cold_progress.py` | 冷启动进度：按 `cold/` 固定分区缓存计数「三表齐备标的 / 目标」（目标取自清单，二级 1222 / 一级 310 自适应；可传清单名覆盖） |
| `industry_l2_top_codes.txt` | **默认**选样清单（131 个申万二级行业 × 前 10 = 1222 只；`#` 行为注释，分组头写「一级 / 二级」；**不入库**，本地生成） |
| `industry_top_codes.txt` | 一级口径选样清单（`--level l1`：31 个申万一级行业 × 前 10 = 310 只；`#` 行为注释与分组） |
| `<清单名>_out.txt` / `_err.txt` | 各次跑批日志（`build_industry_top.py` 启动时生成，追加） |

## 正式跑批入口（在代码仓目录下执行）

```powershell
cd d:\AIwork\PyramidSeek\PyramidSeekApp
$env:PYTHONIOENCODING = 'utf-8'   # 控制台中文乱码时

python -m src.batch --mode <模式> [参数]
```

### 五种模式（D-32；D-40 后 `monthly` / `cold` 停用）

> ⏹ **D-40（2026-10-16）**：行情历史不再采集 → **建仓请用 `financials`**（三表 + 快照、无行情）。
> `cold` 会带 `with_history=True` 抓行情（走东财且会限速），**不要再用于建仓**。

| 模式 | 干什么 | 频率 | 单只成本 |
|---|---|---|---|
| `daily` | 快照（新浪 spot）+ 从标准层读财报重算 + 产物化 | 每日 | 分钟级 |
| ~~`monthly`~~ | ~~行情全历史重抓~~ | — | ⏹ **停用**（D-40） |
| `financials` | **三表（THS）+ 全市场快照，不抓行情** ← **建仓 / 扩池 / 披露季都用这个** | 一次性 / 披露季 | **~9.9s/只**（THS，无限速） |
| ~~`cold`~~ | ~~三表 + 行情历史 + raw + 快照~~ | — | ⏹ **停用**（含行情，与 D-40 冲突） |
| `retry` | 补抓上次失败清单 `data/_pending.json` | 失败后 | 视清单 |

### 常用参数

```
--codes SH600900 SH600019      # 只跑指定标的（冒烟）
--codes-file <路径>            # 清单文件（每行一个代码，# 为注释；与 --codes 合并去重）
--limit N                      # 每市场最多 N 只
--generated-at <UTC ISO8601>   # 固定生成时间即可复现产物（R19；默认当前时间）
--no-validate                  # 跳过产物校验
--offline                      # 只用 fixtures（回归用，--mode 不生效）
```

产物写 `public/data/`，标准层写 `data/standard.duckdb`，校验失败退出码非 0。

## 典型流程

### 冷启动（一次性 + 扩池）

```powershell
cd d:\AIwork\PyramidSeek\PyramidSeekApp   # 代码仓根（本目录的上一级），以下命令都在这里执行
# ① 选样（默认申万二级：131 行业 × 前 10 = 1222 只；跳过「未分类」）并直接后台启动
python batch\build_industry_top.py --run
#    旧一级口径（31 行业 × 前 10 = 310 只）：再加 --level l1

# ② 看进度（只看「三表齐备标的 x / 目标」；行情行已停止增长，不作完成度指标）
python batch\check_cold_progress.py

# ③ 中断后续跑（二选一）：
#    a) python batch\build_industry_top.py --codes-file batch\industry_l2_top_codes.txt
#    b) 直接重跑 build_industry_top.py --run 同一条命令
#    —— 财报缓存固定落 cold/（D-37），已抓的秒过；financials 模式 TTL = 10 天
# ④ 按清单分批控制节奏：改用 --codes-file + --limit，或编辑清单文件只留未完成的
# ⑤ 失败的看日志尾部，之后 --mode retry 补抓

# 想在控制台实时看跑批（不进后台）：
python batch\build_industry_top.py --codes-file batch\industry_l2_top_codes.txt --foreground
# Ctrl+C 中断；重跑同一条命令即续传（cold/ 缓存 TTL 内秒过已抓的）
```

只选部分行业（行业名须属于当前层级，默认二级；传一级名会被拒并提示，清单不动）：
`python batch\build_industry_top.py --level l1 --industries 银行 非银金融 --run`

> 想刷新行业映射（新上市 / 行业调整后重新选样）：删 `PyramidSeekApp\data\raw\akshare\cold\`
> 下的 `list_A.json` 与 `sw_*.json` 再跑选样 —— 这些映射键无 TTL，不会自动过期。

### 扩池 / 新标的

改 `--per-industry` 或换行业重新生成清单 → 只对新增代码联网抓（老的命中 `cold/` 缓存秒过）→ 跑完打一版 `cold/` Release 备份（Runbook §9）。

### 日常节奏（全市场前的 310 只阶段为手工）

```powershell
python -m src.batch --mode daily        # 每天（以后由 Actions/self-hosted 定时，M5）
python -m src.batch --mode monthly      # 每月第一个周六
python -m src.batch --mode financials   # 披露季：5 / 9 / 11 月各一轮
python -m src.batch --mode retry        # 有失败清单时
```

## 注意事项

- ⚠️ **IPv6 黑洞会让跑批慢 10 倍（2026-10-24 实测，本机）**：Sina / 东财 / 连 baidu 的 AAAA 记录都排在 A 记录前面，
  而本机 IPv6「有前缀没出口」→ urllib3 逐地址尝试，每个不通的 IPv6 要等满 21s（`quotes.sina.cn` 有 2 个 →
  **一次建连白等 42.11s**，表现为「一张表 44s、一只 2 分钟」）。akshare 内部 `requests.get` 不传 timeout，
  只能干等，**曾被误判成新浪限速**。
  - 已解（**D-46，写在代码仓里**）：`python -m src.batch` 非 `--offline` 启动时调 `src.net.ensure_ipv4_preferred()`
    —— TCP 探测 2 个目标（最坏 3s），不通就把本进程 `getaddrinfo` 过滤成只返 IPv4。建连 0.03s，
    实测「一张表」46s → 3~5s、一只 138s → ≈13s。
  - 因此**经本脚本启动与直接 `python -m src.batch` 效果一致**，batch 侧不再需要任何补丁。
  - 手动控制：`PYRAMID_IPV6=0` 强制只用 IPv4 / `=1` 强制保留双栈（都跳过探测）。
  - 根治（系统级，需管理员）：`netsh interface ipv6 set prefixpolicy ::ffff:0:0/96 55 4`，或修好本机 IPv6 出口。

- **缓存两层（D-37）**：全历史数据（财报三表 / 行情 qfq+raw）**只落 `data/raw/akshare/cold/`
  固定分区**，新鲜度 = 文件 mtime + 模式 TTL（`cold`=∞ / `monthly`=20 天 / `financials`=10 天），
  超龄才重抓并覆盖写回 —— **超 24h 的长跑批跨天/跨周照常续传**，`cold/` 恒为最新全量
  （删库重建免网、Release 备份随时打即最新）。时点数据（spot 快照 / 列表 / 申万映射）
  仍按日分区 `data/raw/akshare/<日期>/`。
- **重抓粒度 = 每只一整条序列**（接口一次返回全历史；qfq 除权后整段重算），无「只抓最新」。
- **全市场 cold 仍只能本地跑**：按 THS 口径 5,500 只 ≈15h（Actions 单 job 6h 限只容 daily）——M6-5/D-36。
- ⏹ **行情历史不再采集（D-40）**：`monthly` 模式停用，`cold` 不再带 `with_history`。
  akshare 内所有行情接口都挂新浪/东财且都会限速；月K 来源另议（候选 Baostock，未评估）。
- schema 不匹配（Binder Error）：删 `data/standard.duckdb`（含 `.wal`）重跑即可，
  `cold/` 是最新全量，重放本地缓存分钟级重建、完全不打网络（09 Runbook §3.7）。
- 临时实验脚本放项目根 `temp/`，不要混进本目录。
