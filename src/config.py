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
CONTRACT_VERSION = "1.1"  # 产物契约版本（改契约结构必须升版本）

# ── 市场与行业标准（技术说明书 §3.4）─────────────────────────────────────
MARKETS = ("A", "HK")
INDUSTRY_STANDARD = {"A": "申万2021", "HK": "GICS"}
# 港股行业口径降级路径（ADR-011 / 数据源规格 §6）：AkShare 不直接提供 GICS，
# M1 先用东财口径跑通全链路，industry_std 标注为下面这个值；GICS 自建映射独立推进。
INDUSTRY_STANDARD_HK_FALLBACK = "东财口径（待校准 GICS）"
INDUSTRY_UNCLASSIFIED = "未分类"  # 缺失映射的股票归入此组，暂不参与行业分位

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
PERCENTILE_INDICATORS = ("net_profit_scale", "asset_turnover")

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

# ── 行情复权口径（家法 R8 / 数据源规格 §5）───────────────────────────────
ADJUST_MODES = ("qfq", "hfq")  # 前复权（默认）/ 后复权
DEFAULT_ADJUST = "qfq"

# ── 数据质量规则（数据源规格 §9）─────────────────────────────────────────
BALANCE_TOLERANCE = 0.05  # 勾稽：|资产 − (负债 + 权益)| / 资产 ≤ 5%
MOM_CHANGE_THRESHOLD = 2.00  # 环比突变阈值 ±200% → 标记待确认
MARKET_MISSING_RATE_MAX = 0.05  # 单市场缺失率 ≤ 5%，超阈值 → degraded

# ── 数据源容错（数据源规格 §7）───────────────────────────────────────────
FETCH_RETRY_TIMES = 3  # 重试 3 次
FETCH_RETRY_BACKOFF = (1, 3, 9)  # 指数退避（秒）
FETCH_TIMEOUT_SECONDS = 15

# ── 运行环境（家法 R17：本地 / CI 双跑，不写死绝对路径）──────────────────
PS_ENV_LOCAL = "local"
PS_ENV_CI = "ci"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "fixtures"
PUBLIC_DIR = PROJECT_ROOT / "public"
PUBLIC_DATA_DIR = PUBLIC_DIR / "data"
DATA_DIR = PROJECT_ROOT / "data"  # 本地运行产物（已 gitignore）
FIXTURES_FILE = FIXTURES_DIR / "financials.json"

# ── 免责声明（家法 R20：页面 / 产物 notes / Demo 输出三处随行）───────────
DISCLAIMER = "本项目为学习/研究用途，所有评分与数据不构成任何投资建议。"
