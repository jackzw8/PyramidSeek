# 金字塔选股 PyramidSeek

把《HOLDLE 投资交易课》的选股方法论产品化：

```
AkShare（新浪 + 同花顺）  →  离线批处理算分  →  静态 JSON 产物  →  GitHub Pages 静态站点
```

- **定位**：自用 / 小范围；全功能免费；无账号体系（收藏用匿名 `visitorId`）。
- **市场**：A股 + 港股（本期不做美股）。
- **核心口径**：`金字塔分 = clamp(质量分 Q + 成长分 G, 0, 1000)`。
- **架构**：零服务器（GitHub Actions 定时跑批 + Pages 托管），产物是前后端唯一契约。

> **免责声明**：本项目为学习 / 研究用途，所有评分与数据**不构成任何投资建议**。

---

## 一、权威文档（动手前必读）

| 类型 | 位置 |
|---|---|
| 需求 / 技术 / 计划 / 数据字典 / 数据源 / 评分 / 契约 / ADR / Runbook | `../项目文档/技术文档_Markdown/`（编号 01–09） |
| 家法（22 条规范） | `../项目文档/项目总结/01_项目规范.md` |
| 经验沉淀 / 评审发现 | `../项目文档/项目总结/02_项目经验.md` |
| 使用方法 | `../项目文档/项目总结/03_使用方法.md` |

关键约定（与文档一致，改动先改文档）：

- 金字塔分 `clamp(Q + G, 0, 1000)`；Q 由 6 项指标加权（权重合计 1000）。
- 行业对比池：A股申万 / 港股 GICS；二级样本 `< 30` 回退一级；概念板块不纳入；分市场不混排。
- 财报窗口：近 **5 个完整财年（年报）**；财报 **报告期 + 4 个月**生效；季报口径不可比，不进主评分。
- 单位：金额「亿元、2 位小数」，比率存小数；空值是 `NULL` 不是 `0`。
- 版本三件套：`snapshotVersion` / `contractVersion` / `formulaVersion`。
- 港股行业分类按 ADR-011 走降级路径：先用东财口径跑通全链路，`industry_std` 标注「东财口径（待校准 GICS）」。

---

## 二、目录结构

```
PyramidSeekApp/
├── README.md
├── requirements.txt                # 运行依赖（固定版本）
├── requirements-dev.txt            # 开发依赖（pytest / ruff）
├── pyproject.toml                  # ruff + pytest 配置
├── .github/workflows/ci.yml        # CI：lint + pytest
├── src/
│   ├── config.py                   # 权重/锚点/阈值/分类标准（常量唯一来源）
│   ├── datasource/                 # 数据源抽象 + AkShare（新浪 / THS）+ 官方源（对账）
│   ├── pipeline/                   # 采集 / 清洗 / 落库（标准层 + 分析层）
│   ├── engine/                     # 指标 / 评分 / 行业分位
│   ├── build/                      # 产物生成（ranking/stock/meta/derived）+ 校验
│   └── batch.py                    # 总入口：一条命令跑完
├── schema/                         # 产物 JSON Schema（meta/ranking/stock/search-index，校验用）
├── fixtures/                       # 示例数据（离线回归 + 黄金样本输入）
├── public/                         # 前端静态站点（原生 HTML/CSS/JS + 手绘 SVG，零依赖）
│   ├── index.html                  # 排行页（主榜 + 单列，筛选 / 分页 / 组内前 10% 高亮）
│   ├── stock.html                  # 个股详情（评分构成 / 雷达 / 月K·MACD·年ROE / 四张表 / 同业面板）
│   ├── watchlist.html              # 观察池（visitorId + 导出 / 导入）
│   ├── css/style.css
│   ├── js/api.js                   # 产物加载层（fetch + 缓存 + 降级 / 断产物提示）
│   ├── js/common.js                # 公共工具（格式化「—」/ 搜索 / 收藏 / 导航）
│   ├── js/{ranking,stock,watchlist}.js
│   └── data/                       # 产物（跑批生成，gitignore）
├── tests/                          # 单元 / 回归 / 契约测试
└── data/                           # 本地运行产物（gitignore）
```

---

## 三、环境与运行

**依赖**：Python 3.11+（本地已验证 3.14）。

```powershell
# 1) 安装依赖
python -m pip install -r requirements.txt -r requirements-dev.txt

# 2) 离线跑通整链（仅用 fixtures/，不联网；跑完自动做产物校验）
python -m src.batch --offline

# 2b) 联网跑批 —— 模式化（D-32，详见《数据源接入规格》§7.1）：
python -m src.batch --mode daily        # 快照 + 全量重算（日常默认，分钟级）
python -m src.batch --mode financials   # 三表全量重抓（披露季：1-4 月年报、4/8/10 月季报）
python -m src.batch --mode monthly      # 行情历史重抓（qfq+raw，校正前复权序列与 hfq）
python -m src.batch --mode cold         # 冷启动（三表 + 行情历史，本地分批跑）
python -m src.batch --mode retry        # 补上次失败清单（data/_pending.json）

# 3) 运行测试
pytest

# 4) 单独校验已有产物（M3-5；失败非零退出）
python -m src.build.validate [--data public/data]

# 5) 本地预览前端（产物已在 public/data/；浏览器打开 http://localhost:8000/）
cd public
python -m http.server 8000
```

跑批环境由环境变量 `PS_ENV` 区分（`local` | `ci`），脚本不写死绝对路径、不使用交互输入。
需要完全可复现的产物时固定时间戳：`python -m src.batch --offline --generated-at 2026-10-15T00:00:00Z`。

> **数据源与耗时（2026-10-16 定案，见《计划完成情况》D-38 / D-42 / D-44 / D-45）**：
> 财报链序：**新浪三表（主）→ 同花顺 THS（第 2 级）→ fixtures**（三级；官方源移出链、仅对账）；16:53 复测
> 新浪三表 ≈**5.8s/只**、THS ≈7.9s/只（正常档）；含请求节流（D-42，2~3s 抖动）规划口径
> **≈13s/只** → 全市场 5000 只 **≈18h（分天）**。**月K 行情停止跑批**，`monthly` 模式停用
> （akshare 内行情接口均挂新浪/东财且都会限速），产物 `kline` 恒缺省。财报缓存键含源名
> （`financial_{源}_{code}_{slug}`，D-44）。本地验证与前端联调请用
> `--codes` / `--limit` 小样本；原始返回按日缓存（`data/raw/<源>/<日期>/`），**同日重跑秒过**。
> 首次跑批还会建「申万行业映射」（131 个二级行业，约 1 分钟，同样按日缓存）。

产物输出目录（默认）：`public/data/`，含 `meta.json`、`industries.json`、
`ranking/{A,HK}.json`（`rows` 主榜 + `excluded` 单列）、`stock/{code}.json`（一码一文件）。

---

## 四、当前进度（里程碑）

| 里程碑 | 状态 |
|---|---|
| **M0 骨架与规范** | ✅ 仓库结构 / `config.py` / `fixtures` / CI |
| **M1 数据层** | ✅ 数据源抽象 → AkShare → 官方源兜底 → 清洗 → DuckDB/Parquet |
| **M2 计算层** | ✅ 指标 / 子分 / 成长分 / 行业分位 / 组装总分 / 双引擎黄金样本对账 |
| **M3 产物层** | ✅ ranking（主榜 + 单列）/ stock / meta 产物 + schema 校验 + 跑批总入口 |
| **M4 前端** | ✅ 排行（主榜 + 单列、组内前 10% 高亮）/ 详情（雷达 / 月K·MACD / 四表 / 同业）/ 观察池 / 拼音搜索；数据全部来自产物 |
| M5 发布 | ⬜ 待开工（Actions 跑批 + Pages 部署） |
| M6 打磨 | ⬜ 待开工（告警 / 缓存 / 降级提示 / 文档） |

---

## 五、开发纪律（摘自家法 R1–R22，详见 `01_项目规范.md`）

1. 改行为先改文档，再改代码；改契约升 `contractVersion`，改公式升 `formulaVersion`。
2. 常量只有一个家：Python 在 `src/config.py`，JS 在 `engine.js` 顶部常量区。
3. 双引擎（Python / JS）逐字对齐，用同一份 `fixtures` 做黄金样本对账，CI 强制。
4. 离线永远可跑：任何改动不得破坏「仅用 `fixtures/` 跑通整链」。
5. 跑批幂等、产物不手改；产物只由 `batch` 生成。
6. **按需**开任务分支（判据：做坏了能否干净回退）；评分产物提交到独立 `data` 分支（强制单提交）。
