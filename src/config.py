"""PyramidSeek 全局配置常量（家法 R12：常量只有一个家）。

依据文档（改动先改文档，再改本文件）：
- 《产品技术说明书》§3.3 数据口径 / §4 计算层 / §5 产物契约
- 《评分算法规格》§3 指标 / §4 质量分 / §5 对比池 / §6 成长分 / §7 合成排序
- 《数据源接入规格》§7 容错 / §9 数据校验
- 《产物契约与 JSON Schema》§1 契约原则 / §3 字段

禁止在业务逻辑里硬编码权重、锚点、阈值与版本号。
"""

from __future__ import annotations

from pathlib import Path

# ── 版本（家法 R2 / R3 / R15）────────────────────────────────────────────
FORMULA_VERSION = "v1.0"  # 评分公式版本（改评分参数必须升版本）
# 产物契约版本（改契约结构必须升版本）：1.2 = M3 口径（rows 收窄为主榜 + excluded 单列 +
# cash_ratio/debt_ratio 列 + §3.5 数值精度），见《产物契约》V1.4 变更记录。
CONTRACT_VERSION = "1.2"

# ── 市场与行业标准（技术说明书 §3.4）─────────────────────────────────────
MARKETS = ("A", "HK")
INDUSTRY_STANDARD = {"A": "申万2021", "HK": "GICS"}
# 港股行业口径降级路径（ADR-011 / 数据源规格 §6）：AkShare 不直接提供 GICS，
# M1 先用东财口径跑通全链路，industry_std 标注为下面这个值；GICS 自建映射独立推进。
INDUSTRY_STANDARD_HK_FALLBACK = "东财口径（待校准 GICS）"
# D-58：港股行业实装口径 —— 东财 `stock_hk_company_profile_em` 的「所属行业」，仅港股通成分
INDUSTRY_STANDARD_HK_GGLT = "东财行业（港股通）"
INDUSTRY_UNCLASSIFIED = "未分类"  # 缺失映射的股票归入此组，暂不参与行业分位
# 金融类判定（《评分算法规格》§8「金融类：毛利率/周转率不适用 → 单列」）：
# 按**申万一级**行业名判定（A 股行业口径 = 申万2021）。
# 金融股：毛利率 / 资产周转不适用（R9 的「不适用指标」豁免依据）。
# 前半是申万一级（A 股），后半是东财行业（港股，D-58 口径）——两者命名不同，
# 合并成一个集合，判定时按各自市场的 l1_name 匹配即可（不会互相误伤）。
FINANCIAL_SECTORS = frozenset({"银行", "非银金融", "保险", "其他金融"})

# 行业均值填充的最小样本数（D-65）：池内某指标的有效样本少于该值时**不填充** ——
# 否则会出现「1 只有值、其余 13 只全用它的毛利率」这种荒谬情况（实测保险池）。
# 不足时记为 unfilled：金融股仍豁免，非金融股会因「关键指标缺失」进单列。
MIN_FILL_SAMPLES = 3

# ── 质量分 Q：6 项指标权重（合计 1000，评分规格 §4.1）────────────────────
WEIGHTS: dict[str, int] = {
    "roe": 300,  # 核心
    "net_profit_scale": 180,  # 重要（走行业分位）
    "roa": 160,  # 重要
    "gross_margin": 130,  # 辅助
    "cash_ratio": 120,  # 辅助
    "asset_turnover": 110,  # 辅助（走行业分位）
}
Q_MIN, Q_MAX = 0.0, 1000.0
Q_DECIMALS = 2

# ── 子分映射（评分规格 §4.2）────────────────────────────────────────────
SUB_SCORE_MIN, SUB_SCORE_MAX = 0.0, 100.0
# 绝对值锚点：(指标值, 子分)，分段线性插值，超界截断到 [0, 100]。
ABS_ANCHORS: dict[str, list[tuple[float, float]]] = {
    "roe": [(0.00, 0.0), (0.10, 50.0), (0.20, 80.0), (0.35, 100.0)],
    "gross_margin": [(0.00, 0.0), (0.20, 50.0), (0.30, 80.0), (0.40, 100.0)],
    "net_margin": [(0.00, 0.0), (0.10, 50.0), (0.20, 80.0), (0.30, 100.0)],
    "cash_ratio": [(0.00, 0.0), (0.10, 50.0), (0.25, 80.0), (0.35, 100.0)],
    "roa": [(0.00, 0.0), (0.05, 50.0), (0.10, 80.0), (0.15, 100.0)],
}
# 行业分位映射：子分 = 跑赢同业比例 × PERCENTILE_SCALE
PERCENTILE_SCALE = 100.0
# 走行业分位的指标（其余走绝对值锚点）
# `eps` 一并走分位：§4.2 两种方式都没点它的名，但雷达六维需要它的子分，
# 参考实现 engine.js 用的是分位（`pctScore(epv, ind.eps)`）→ 2026-10-14 定案（D-22）。
PERCENTILE_INDICATORS = ("net_profit_scale", "asset_turnover", "eps")
# 分位分子分取整位数：对齐 engine.js 的 `toFixed(2)`（D-18）
PERCENTILE_DECIMALS = 2

# ── 成长分 G（评分规格 §6）───────────────────────────────────────────────
GROWTH_MAX = 100.0  # 单项上限（方向分上限）
GROWTH_MIN_TOTAL, GROWTH_MAX_TOTAL = -200.0, 200.0  # G = 净利润项 + 现金流项
CAGR_CAP = 0.20  # |CAGR| ≥ 20% → 方向分满分
CV_LOW = 0.15  # CV ≤ 0.15 → 稳定性系数 k = 1.0
CV_HIGH = 0.75  # CV ≥ 0.75 → k = K_MIN
K_MIN, K_MAX = 0.6, 1.0
STABILITY_DECIMALS = 4  # k 的中间结果保留位数（双引擎逐字对齐要求）
GROWTH_ITEM_DECIMALS = 2  # 单项成长分 round 位数（先 round 再相加，禁止先加总）

# ── 总分与排序（评分规格 §7）─────────────────────────────────────────────
SCORE_MIN, SCORE_MAX = 0.0, 1000.0
SCORE_DECIMALS = 2
TOP_PCT = 0.10  # 组内前 10% 高亮

# ── 行业对比池（评分规格 §5 / 家法 R6）───────────────────────────────────
INDUSTRY_FALLBACK_MIN = 30  # 二级样本 < 30 时回退一级（唯一来源，禁止调用处写死）
COMPARE_LEVEL_DEFAULT = "L2"
COMPARE_LEVEL_FALLBACK = "L1"

# ── 数据窗口与单位口径（技术说明书 §3.3 / 家法 R7 / R10）─────────────────
YEARS = 5  # 主评分取近 5 个完整财年（年报）
EFFECTIVE_LAG_MONTHS = 4  # 财报「报告期 + 4 个月」生效（家法 R9）
AMOUNT_UNIT = "亿元"
AMOUNT_DECIMALS = 2
RATIO_DECIMALS = 4  # 汇率/比率展示精度（内部统一存小数）
# 产物展示精度（《产物契约》§3.5）：金额 2 位 / 比率 4 位（上面两项），以下为特例。
EPS_DECIMALS = 2  # 每股收益（元/股，非「亿元」类）
PRICE_DECIMALS = 4  # 月K 价格（前复权）

ADJ_FACTOR_DECIMALS = 6  # 复权因子精度（《数据字典》§5 DDL：DECIMAL(18,6)）

# ── 行情复权口径（家法 R8 / 数据源规格 §5）───────────────────────────────
# raw = 不复权原始价（家法 R8「原始价单独保留，不删除」）；月K/MACD 一律用 qfq。
ADJUST_MODES = ("qfq", "hfq", "raw")
DEFAULT_ADJUST = "qfq"

# ── 历史行情采集开关（2026-11-04，D-56 / M4-7）────────────────────────────
# 行情改由**前端按需取**（详情页打开时取这一只的日K/月K，腾讯接口 CORS `*`），
# 跑批不再采集历史序列 —— 全市场 5000 只的采集成本（D-40）从此与项目无关。
# 保留 False 作为硬闸：任何路径想抓历史行情都必须先显式改这里，避免误取。
# 注意：`market_snapshot`（全市场快照）**不在此列** —— 它承担停牌判定与
# `dataDate.price`，每天 1~2 次调用，必须继续跑。
HISTORY_FETCH_ENABLED = False

# ── 数据质量规则（数据源规格 §9）─────────────────────────────────────────
BALANCE_TOLERANCE = 0.05  # 勾稽：|资产 − (负债 + 权益)| / 资产 ≤ 5%
MOM_CHANGE_THRESHOLD = 2.00  # 环比突变阈值 ±200% → 标记待确认
MARKET_MISSING_RATE_MAX = 0.05  # 单市场缺失率 ≤ 5%，超阈值 → degraded

# ── 数据源与回退链（数据源规格 §1 / §7）──────────────────────────────────
PRIMARY_SOURCE_NAME = "akshare"  # 主源名（回退链判定 degraded 的基准）
FALLBACK_SOURCE_NAMES = ("official", "fixture")  # 官方源兜底 / fixtures 离线回退

# ── PDF 文本抽取（M1-7 / D-24）────────────────────────────────────────────
# 按序尝试：前者抽不出内容（空串）或未安装时自动回落后者。
# `pymupdf` 对「无 ToUnicode 映射的 CID 中文字体」能正确解码（pypdf 只能输出错码位）——
# 这是港股年报的普遍情况（D-24 实测：pypdf 抽出 0 个 CJK 字符，pymupdf 抽出 13 万个）。
# **注意 `pymupdf` 是 AGPL 许可**：只用于本地跑批工具链，不进入发布产物；
# 未安装时自动回落 `pypdf`（A 股年报用 pypdf 一直正常）。
PDF_EXTRACTORS = ("pymupdf", "pypdf")

# ── 数据源容错（数据源规格 §7 / §8）──────────────────────────────────────
FETCH_RETRY_TIMES = 3  # 重试 3 次
FETCH_RETRY_BACKOFF = (1, 3, 9)  # 退避上限序列（秒）：3 次重试实际等待 2 次（1s、3s）
FETCH_TIMEOUT_SECONDS = 15  # 单次请求超时（数据源规格 §7）
FETCH_MIN_INTERVAL_SECONDS = 1.0  # 官方源请求间隔下限（§8：遵守目标站点抓取规范）
# 请求节流（D-42）：AkShare 不内置限流（官方文档"建议合理设置请求间隔"）；上游阈值
# （社区实测）—— 新浪单 IP 45~65 次/60 秒（滑动窗口、无 Retry-After）、东财约 3 次/秒、
# 同花顺 hexin-v 每值仅 5~10 次；超阈值触发 5~15 分钟阶梯式封禁（表现为每请求被压到
# 43~44s / 149s 的等间隔惩罚，见《计划完成情况》D-38 / D-42）。
# 取 2~3 秒随机抖动 → 20~30 请求/60 秒，稳在阈值内；**只对真发请求生效**（缓存命中不触发）。
FETCH_INTERVAL_JITTER_SECONDS: tuple[float, float] = (2.0, 3.0)
# 申万行业成分（约 131 个二级行业 / 全市场）：单次通常 ~0.5s，但连续请求会被限流 →
# 需要请求间隔；抖动时给中等超时（默认 15s 会误杀，见 D-8）。逐个失败只跳过该行业。
INDUSTRY_CONS_TIMEOUT_SECONDS = 120
INDUSTRY_CONS_INTERVAL_SECONDS = 0.5

# 港股「所属行业」逐只查询间隔（D-58）：东财 `stock_hk_company_profile_em` 单次 ~0.6s，
# 站点阈值约 3 次/秒 → 1.2s 间隔稳在阈值内。仅港股通成分会用到（667 只 ≈ 13 分钟）。
HK_INDUSTRY_INTERVAL_SECONDS = 1.2

# 「单股批量接口」的中等超时：三表（实测 ~44s/张）与行情（~22s/次）远超默认 15s，
# 但也不该套用 1800s 的「全量翻页」上限 —— 目标站点挂起时会白等半小时（D-8 实跑踩到）。
FETCH_TIMEOUT_MEDIUM_SECONDS = 300

# ── IPv6 自检（D-46）─────────────────────────────────────────────────────
# 本机 IPv6 可能「有前缀、没出口」：DNS 有 AAAA、本机也配了全局地址，但包发到网关就没了。
# 此时 urllib3 会按 getaddrinfo 顺序逐个地址建连，每个不通的 IPv6 要等满 Windows 的
# SYN 重试上限 21s（quotes.sina.cn 有 2 个 AAAA → 一次建连白等 42s）。
# 入口（src.batch，非 --offline）启动时探测一次；不通就把本进程的 getaddrinfo 过滤成 IPv4。
# 手动控制：环境变量 PYRAMID_IPV6=0 强制只用 IPv4 / =1 强制保留 IPv6（两者都跳过探测）。
IPV6_PROBE_TARGETS = ("quotes.sina.cn", "www.baidu.com")  # 都带 AAAA 的稳定目标
IPV6_PROBE_PORT = 443
IPV6_PROBE_TIMEOUT_SECONDS = 1.5  # 单地址建连超时（死链路要 21s 才超时，必须自己卡短）
IPV6_PROBE_MAX_ADDRESSES = 1  # 每个目标试 1 个地址即可：两个独立目标都失败才判不通
HTTP_USER_AGENT = "PyramidSeek/0.1 (personal research; contact: local)"
# 全量列表类接口（内部翻页）实测需十几分钟，见《计划完成情况》D-10；
# 15s 是"单次请求"约束，套到批量接口上会让它必败，故单独给一个宽松上限。
FETCH_TIMEOUT_SLOW_SECONDS = 1800

# ── 历史数据缓存新鲜度（D-37：固定分区 cold/ + 文件 mtime TTL）────────────
# financials / monthly 重跑时，cold/ 里全历史数据的 mtime 在此期限内 → 直接命中不重抓；
# 超龄才重抓并覆盖写回（mtime 刷新）。全市场跑批 67~202h 必然跨天，判新鲜必须与
# 「同一天」解耦（D-34 的按日分区口径已废弃）。cold 模式 TTL = ∞（基线永远命中）。
MONTHLY_FRESH_DAYS = 20  # 月度节奏下下次跑 ≈30 天 > 20 天 → 正常重抓；中断 3 周内可续传

# 财报 TTL 分档（D-52）：披露季短、淡季长 —— 财报只在披露日变化，淡季放宽不会漏更新。
# 窗口按**北京日期**判定（UTC+8，无夏令时）；起止都含端点，各留 5 天缓冲覆盖提前/延期披露。
DISCLOSURE_WINDOWS: tuple[tuple[str, tuple[int, int], tuple[int, int]], ...] = (
    ("年报/一季报", (1, 1), (5, 5)),  # 法定 4/30
    ("半年报", (7, 1), (9, 5)),  # 法定 8/31
    ("三季报", (10, 1), (11, 5)),  # 法定 10/31
)
FINANCIALS_FRESH_DAYS = 10  # 披露季：保跟手（同时也是长跑批的续跑窗口）
FINANCIALS_FRESH_DAYS_OFF_SEASON = 30  # 淡季：财报不变，只兜「更正 / 延期披露」

# ── 运行环境（家法 R17：本地 / CI 双跑，不写死绝对路径）──────────────────
PS_ENV_LOCAL = "local"
PS_ENV_CI = "ci"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = PROJECT_ROOT / "schema"  # 产物 JSON Schema（《产物契约》§5，M3-5 校验用）
FIXTURES_DIR = PROJECT_ROOT / "fixtures"
# 站点与运行时数据统一收在 assets/ 下（2026-11-05 用户要求，减少根目录噪音）：
#   assets/public —— 前端站点（页面入库，产物 gitignore）
#   assets/dist   —— 发布快照（gitignore）
#   assets/data   —— DuckDB / 缓存 / 清单 / 跑批日志（gitignore）
ASSETS_DIR = PROJECT_ROOT / "assets"
PUBLIC_DIR = ASSETS_DIR / "public"
PUBLIC_DATA_DIR = PUBLIC_DIR / "data"
DATA_DIR = ASSETS_DIR / "data"  # 本地运行产物（已 gitignore）
FIXTURES_FILE = FIXTURES_DIR / "financials.json"

# ── 免责声明（家法 R20：页面 / 产物 notes / Demo 输出三处随行）───────────
DISCLAIMER = "本项目为学习/研究用途，所有评分与数据不构成任何投资建议。"
