"""数据源抽象接口（任务 M1-1；依据《数据源接入规格》§2 + 《数据字典》§3）。

本模块是 M1-1 的交付物：**接口文档齐全**——方法签名、参数口径、返回字段、异常约定
四件事全部写死在此，任何实现（AkShare 主源 / 官方源兜底 / 未来的商业源 / fixtures 回退）
都必须满足同一份契约。

── 调用约定 ─────────────────────────────────────────────────────────────
1. **失败必须抛 ``DataSourceError``**，禁止吞掉异常返回空列表。上层
   （``pipeline.fetch``，M1-6）靠它决定回退链：
   新浪主源（AkShare）→ 同花顺 THS → 官方源（巨潮/披露易）→ ``fixtures/``
   （家法 R16：离线永远可跑）。
2. 返回的是「**标准层**」记录（《数据字典》§3），不是数据源原始字段；
   字段映射（元 → 亿元、代码加市场前缀等）由实现内部完成。
3. 单位与空值口径（家法 R10 / R11）：金额「亿元」保留 2 位小数、比率存**小数**、
   日期 ``YYYY-MM-DD``、时间戳 UTC ISO8601；**缺失用 ``None``，绝不用 0 代替**
   （0 是有效值）。
4. 行情同时保留原始价与复权价：``close`` 为不复权价、``close_qfq`` 供月K / MACD
   使用（家法 R8：默认前复权）。

── 方法一览 ─────────────────────────────────────────────────────────────
============================  =============================  =====================
方法                            返回                           字段契约
============================  =============================  =====================
``list_stocks(market)``       股票主数据 + 行业归属           ``STOCK_*_FIELDS``
``financials(code)``          近 N 年年报/中报（标准化后）     ``FINANCIAL_*_FIELDS``
``price_daily(code, adjust)`` 日行情（含复权价）              ``PRICE_*_FIELDS``
``industry_tree(market)``     行业分类树（申万 / GICS）       ``INDUSTRY_*_FIELDS``
``health_check()``            连通性自检                      ``bool``
============================  =============================  =====================

── 实现清单（M1）────────────────────────────────────────────────────────
- ``akshare_src.AkShareSource``（M1-2，主源，含重试 / 退避 / 缓存）
- ``official_src.OfficialSource``（M1-3，巨潮 / 披露易兜底）
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .. import config

# ── 参数取值（口径常量见 config，禁止调用处硬编码）────────────────────────
MARKETS = config.MARKETS  # ("A", "HK")
ADJUST_MODES = config.ADJUST_MODES  # ("qfq", "hfq", "raw")
DEFAULT_ADJUST = config.DEFAULT_ADJUST  # "qfq"

# ── 返回字段契约（来源：《数据字典》§3，必填 / 可选分开声明）──────────────
# stock：股票主数据（《数据字典》§3.1）
STOCK_REQUIRED_FIELDS = (
    "code",  # 主键，如 SH600900（市场前缀 SH/SZ/BJ/HK）
    "name",
    "market",  # A / HK
    "exchange",  # SSE / SZSE / BSE / HKEX
    "l1_code",
    "l1_name",  # 申万一级 / GICS Sector
    "l2_code",
    "l2_name",  # 申万二级 / GICS Industry
    "industry_std",  # 申万2021 / GICS（港股降级路径见 ADR-011）
    "status",  # normal / ST / suspended / delisted / new
    "is_financial",  # 是否金融类（毛利率/周转率不适用）
    "updated_at",
)
STOCK_OPTIONAL_FIELDS = (
    "listed_at",  # 上市日期；次新股判定用（<5 年）
    "shares",  # 总股本（亿股），算 EPS 用
)

# financial_report：财务报表（《数据字典》§3.2）
FINANCIAL_REQUIRED_FIELDS = (
    "code",
    "report_period",  # 报告期，如 2024-12-31
    "report_type",  # annual / h1 / q1 / q3
    "is_cumulative",  # 季报为累计口径 = True（不可比，不进主评分，家法 R7）
    "revenue",  # 亿元
    "net_profit",  # 归母净利润，亿元
    "total_assets",  # 亿元
    "equity",  # 归母股东权益，亿元
    "op_cashflow",  # 经营活动现金流净额，亿元
    "effective_date",  # 报告期 + 4 个月（家法 R9）
    "source",  # akshare / cninfo / hkexnews / fixture
    "fetched_at",
)
FINANCIAL_OPTIONAL_FIELDS = (
    "gross_profit",  # 毛利（缺则 revenue − 营业成本；金融类不适用 → None）
    "total_liabilities",  # 勾稽校验用：|资产 − (负债 + 权益)| / 资产 ≤ 5%
    "cash",  # 现金及现金等价物
    "icf",  # 投资活动现金流净额
    "fcf",  # 筹资活动现金流净额
    # 报表币种（ISO 4217 三字母：CNY / HKD / USD）—— 2026-10-14 加（D-24）：
    # 港股可能是人民币 / 港币 / 美元报表，金额一律"亿元"但**币种不能混**。
    # 可选：A 股缺省按 CNY 由清洗阶段补齐；港股由年报正文识别（人民幣 → CNY）。
    "currency",
)

# price_daily：日行情（《数据字典》§3.3）
PRICE_REQUIRED_FIELDS = (
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",  # 不复权价
    "adj_factor",  # 复权因子
    "close_qfq",  # 前复权收盘（K线 / MACD 用）
)
PRICE_OPTIONAL_FIELDS = ("volume",)  # 成交量（股）

# industry_node：行业分类树（《数据字典》§3.4）
INDUSTRY_REQUIRED_FIELDS = (
    "standard",  # 申万2021 / GICS
    "version",  # 分类版本号（保证可复现）
    "level",  # 1 / 2
    "node_code",
    "node_name",
)
INDUSTRY_OPTIONAL_FIELDS = (
    "parent_code",  # 上级行业（一级为空）
    "member_count",  # 该节点成分股数量（用于判断是否触发 <30 回退）
)


class DataSourceError(RuntimeError):
    """数据源可识别异常：网络失败 / 超时 / 限流 / 解析失败 / 接口改版。

    ``source`` 与 ``code`` 用于上层记录"哪只股票、哪个源"失败，便于产出
    ``degraded`` 标记（《数据源接入规格》§7）。
    """

    def __init__(self, message: str, *, source: str | None = None, code: str | None = None) -> None:
        self.source = source
        self.code = code
        super().__init__(message)


@runtime_checkable
class DataSource(Protocol):
    """统一数据源接口：便于在 AkShare / 官方源 / 未来商业源之间替换。"""

    name: str

    def list_stocks(self, market: str) -> list[dict[str, Any]]:
        """拉取股票列表 + 行业归属。

        :param market: ``"A"`` 或 ``"HK"``（见 ``config.MARKETS``）。
        :returns: 每条记录含 ``STOCK_REQUIRED_FIELDS`` 全部字段 + ``STOCK_OPTIONAL_FIELDS``。
        :raises DataSourceError: 拉取或解析失败。
        """
        ...

    def financials(self, code: str) -> list[dict[str, Any]]:
        """拉取近 N 年财报（N = ``config.YEARS``，另带最新中报/季报用于趋势）。

        :param code: 带市场前缀的代码，如 ``SH600900``。
        :returns: 每条记录含 ``FINANCIAL_REQUIRED_FIELDS``；季报必须标
            ``is_cumulative=True``。
        :raises DataSourceError: 拉取或解析失败。
        """
        ...

    def price_daily(self, code: str, adjust: str = DEFAULT_ADJUST) -> list[dict[str, Any]]:
        """拉取日行情。

        :param code: 带市场前缀的代码。
        :param adjust: ``"qfq"``（前复权，默认，家法 R8）/ ``"hfq"``（后复权）/
            ``"raw"``（不复权）。单次调用只返回**一种**口径：``close`` 仅 ``raw`` 时有值、
            ``close_qfq`` 仅 ``qfq`` 时有值。标准层要求两者同时存在，因此**清洗阶段（M1-4）
            需按 ``raw`` 与 ``qfq`` 各取一次、按交易日合并**并反算 ``adj_factor``。
        :returns: 每条记录含 ``PRICE_REQUIRED_FIELDS`` + ``PRICE_OPTIONAL_FIELDS``。
        :raises ValueError: ``adjust`` 不在 ``ADJUST_MODES`` 内。
        :raises DataSourceError: 拉取或解析失败。
        """
        ...

    def industry_tree(self, market: str) -> list[dict[str, Any]]:
        """拉取行业分类树（A股申万 / 港股 GICS）。

        :param market: ``"A"`` 或 ``"HK"``。
        :returns: 每条记录含 ``INDUSTRY_REQUIRED_FIELDS`` + ``INDUSTRY_OPTIONAL_FIELDS``；
            一级节点 ``level=1`` 且 ``parent_code=None``。
        :raises DataSourceError: 拉取或解析失败。
        """
        ...

    def market_snapshot(self, market: str) -> list[dict[str, Any]]:
        """拉取**全市场当日快照**（新浪系，1 次调用覆盖全市场；D-32）。

        数据是**原始价**（未复权）且只有当前值 → 用途严格限定（05 规格 §7.1）：
        行情截止日、**停牌判定**（最新价缺失）与「最新价」，不得替代前复权历史。

        :param market: ``"A"``（``stock_zh_a_spot``）/ ``"HK"``（``stock_hk_spot``）。
        :returns: 每条记录 ``{code, name, latest_price, open, high, low, prev_close, volume}``，
            ``code`` 已归一化为带市场前缀的标准形式（``SH600519`` / ``HK00700``）；
            停牌标的 ``latest_price`` 为 ``None``。
        :raises ValueError: ``market`` 不在 ``config.MARKETS`` 内。
        :raises DataSourceError: 拉取或解析失败。
        """
        ...

    def health_check(self) -> bool:
        """连通性自检：能否访问该数据源（不抛异常，返回布尔）。"""
        ...


__all__ = [
    "ADJUST_MODES",
    "DEFAULT_ADJUST",
    "FINANCIAL_OPTIONAL_FIELDS",
    "FINANCIAL_REQUIRED_FIELDS",
    "INDUSTRY_OPTIONAL_FIELDS",
    "INDUSTRY_REQUIRED_FIELDS",
    "MARKETS",
    "PRICE_OPTIONAL_FIELDS",
    "PRICE_REQUIRED_FIELDS",
    "STOCK_OPTIONAL_FIELDS",
    "STOCK_REQUIRED_FIELDS",
    "DataSource",
    "DataSourceError",
]
