"""清洗与标准化（任务 M1-4；依据《数据字典》§4 口径约定、§7 质量规则、§9 校验规则）。

**职责边界**：输入是数据源产出的**标准层记录**（datasource 层已完成字段映射与
元 → 亿元换算），本模块只做**校验 / 补齐 / 去重 / 排序 / 统计**，不重复做字段映射。

实现的口径（家法 R7 / R9 / R10 / R11）：
- 空值语义：``None`` 是唯一空值，**0 是有效值**（R11）；必填为空 → 该行/该股入「待补」；
- 财报窗口：主评分只取**最近 5 个完整财年（annual）**；中报/季报是累计口径不可比，
  只保留最新一条用于趋势提示（R7）；
- 时效：``effective_date = 报告期 + 4 个月``，未生效的不参与评分（R9）——
  本模块只**标注**，是否剔除由计算层按 ``effective_date <= today`` 判断；
- 勾稽：``|资产 − (负债 + 权益)| / 资产 ≤ 5%``，超阈值标异常（不丢弃，留待人工确认）；
- 环比突变：关键字段 ±200% 标记待确认；
- 覆盖度：单市场缺失率 > 5% → ``degraded``（产物 ``meta.degraded`` 用它）。

异常一律**记日志 + 记 Issue**（验收要求"异常有日志"），绝不静默吞掉。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .. import config

# 通用工具取自 leaf 模块 src/common.py（2026-10-14 由 akshare_src 提取，见《计划完成情况》D-12）
from ..common import is_missing, market_of, normalize_code, report_type_of

logger = logging.getLogger(__name__)

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"

# 环比/同比突变的监控字段（《数据字典》§7）
WATCH_FIELDS = ("revenue", "net_profit", "op_cashflow", "total_assets")
# 财报必填字段（《数据字典》§3.2 必填列）
FINANCIAL_REQUIRED = (
    "code",
    "report_period",
    "report_type",
    "revenue",
    "net_profit",
    "total_assets",
    "equity",
    "op_cashflow",
)
# 股票必填字段（《数据字典》§3.1 必填列）
STOCK_REQUIRED = (
    "code",
    "name",
    "market",
    "exchange",
    "l1_name",
    "l2_name",
    "industry_std",
    "status",
)
STATUS_VALUES = ("normal", "ST", "suspended", "delisted", "new")


@dataclass(frozen=True)
class Issue:
    """一条数据质量问题（验收要求"异常有日志"，日志之外还要能结构化统计）。"""

    level: str  # LEVEL_ERROR / LEVEL_WARN
    field: str
    message: str
    code: str | None = None


@dataclass
class CleanResult:
    """清洗结果（结构稳定：字段只增不减，供 M1-5 落库 / M1-6 编排消费）。"""

    stocks: list[dict[str, Any]] = field(default_factory=list)
    financials: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    prices: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)  # 必填缺失 → 待补，不参与评分
    rejected: list[str] = field(default_factory=list)  # 外键不通过 → 丢弃
    coverage: dict[str, float] = field(default_factory=dict)  # market → 缺失率
    degraded: bool = False

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == LEVEL_ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == LEVEL_WARN]


def _today() -> date:
    return datetime.now(UTC).date()


def _missing_fields(row: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    """必填字段里为空（``None`` / ``NaN`` / 空串）的那些（0 不算空，家法 R11）。"""
    missing: list[str] = []
    for name in required:
        value = row.get(name)
        if is_missing(value) or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


def _to_date(value: Any) -> date | None:
    if is_missing(value):
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _effective_date_of(report_period: str) -> str:
    """报告期 + 4 个月（家法 R9）；截断到该月最后一天。"""
    year, month, _day = (int(part) for part in report_period.split("-"))
    month += config.EFFECTIVE_LAG_MONTHS
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    last = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    if month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
        last = 29
    return f"{year:04d}-{month:02d}-{last:02d}"


# ── 股票主数据 ────────────────────────────────────────────────────────────
def normalize_stock(row: dict[str, Any], *, today: date | None = None) -> tuple[dict, list[Issue]]:
    """归一一只股票的主数据：必填校验 + 状态补齐 + 未分类兜底。

    - ``status``：名称判定的结果（ST / delisted）优先保留；否则按上市年限判**次新股**（``new``），
      上市不足 ``config.YEARS`` 年即打标（家法：次新股单列、不参与主榜）；
      ``suspended`` 需要行情，数据源不提供 → 留给 M1-6 用行情补齐（此处不臆造）。
    - 行业缺失 → 归入 ``config.INDUSTRY_UNCLASSIFIED``（《数据源接入规格》§6）。
    """
    today = today or _today()
    issues: list[Issue] = []
    code = row.get("code")

    missing = _missing_fields(row, STOCK_REQUIRED)
    if missing:
        issues.append(
            Issue(LEVEL_ERROR, ",".join(missing), f"股票主数据必填字段缺失：{missing}", code)
        )
    if code and str(code) != normalize_code(str(code), market_of(str(code))):
        issues.append(Issue(LEVEL_ERROR, "code", f"代码格式不规范：{code}", str(code)))
    status = row.get("status")
    if status is not None and status not in STATUS_VALUES:
        issues.append(Issue(LEVEL_ERROR, "status", f"status 取值非法：{status}", code))

    cleaned = dict(row)
    if is_missing(cleaned.get("l1_name")):
        cleaned["l1_name"] = config.INDUSTRY_UNCLASSIFIED
    if is_missing(cleaned.get("l2_name")):
        cleaned["l2_name"] = config.INDUSTRY_UNCLASSIFIED

    listed_at = _to_date(cleaned.get("listed_at"))
    if listed_at is not None and cleaned.get("status") == "normal":
        if listed_at > today - timedelta(days=365 * config.YEARS):
            cleaned["status"] = "new"
            issues.append(
                Issue(
                    LEVEL_WARN,
                    "status",
                    f"上市不足 {config.YEARS} 年（{listed_at}）→ 标记为次新股，单列不参与主榜",
                    code,
                )
            )
    return cleaned, issues


# ── 财报 ──────────────────────────────────────────────────────────────────
def check_balance(row: dict[str, Any]) -> Issue | None:
    """勾稽校验：|资产 − (负债 + 权益)| / 资产 ≤ ``config.BALANCE_TOLERANCE``。"""
    assets = row.get("total_assets")
    liabilities = row.get("total_liabilities")
    equity = row.get("equity")
    if is_missing(assets) or is_missing(liabilities) or is_missing(equity):
        return None  # 缺项不做勾稽（非必填项，见《数据字典》§3.2）
    if not assets:
        return None
    gap = abs(float(assets) - (float(liabilities) + float(equity))) / abs(float(assets))
    if gap > config.BALANCE_TOLERANCE:
        return Issue(
            LEVEL_WARN,
            "total_assets",
            f"勾稽异常：资产 {assets} ≠ 负债 {liabilities} + 权益 {equity}（偏差 {gap:.1%}）",
            row.get("code"),
        )
    return None


def detect_changes(rows: list[dict[str, Any]], *, field_name: str) -> list[Issue]:
    """环比突变检测：相邻期变化超过 ``config.MOM_CHANGE_THRESHOLD``（±200%）标待确认。"""
    issues: list[Issue] = []
    for previous, current in zip(rows, rows[1:], strict=False):
        before = previous.get(field_name)
        after = current.get(field_name)
        if is_missing(before) or is_missing(after) or not before:
            continue
        change = abs(float(after) - float(before)) / abs(float(before))
        if change > config.MOM_CHANGE_THRESHOLD:
            issues.append(
                Issue(
                    LEVEL_WARN,
                    field_name,
                    f"{field_name} 环比突变 {change:.1%}（{before} → {after}，"
                    f"{previous.get('report_period')} → {current.get('report_period')}）",
                    current.get("code"),
                )
            )
    return issues


def normalize_financial_rows(
    code: str,
    rows: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], list[Issue]]:
    """归一一只股票的财报序列。

    步骤（顺序即口径）：必填校验 → 去重 → 补 ``effective_date`` → 时效标注 →
    勾稽/环比校验 → 窗口裁剪（annual 近 ``config.YEARS`` 年 + 最新一条非年报）。
    """
    today = today or _today()
    issues: list[Issue] = []
    kept: dict[tuple[str, str], dict[str, Any]] = {}

    for row in rows:
        item = dict(row)
        item.setdefault("code", code)
        missing = _missing_fields(item, FINANCIAL_REQUIRED)
        if missing:
            issues.append(
                Issue(
                    LEVEL_ERROR,
                    ",".join(missing),
                    f"财报必填字段缺失：{missing}，该期入「待补」",
                    code,
                )
            )
            continue

        # 标准层的报告期必须是 YYYY-MM-DD（《数据字典》§4）；这里只做解析与校验，
        # 不做原始格式猜测 —— 那属于数据源层的字段映射职责。
        period_date = _to_date(item.get("report_period"))
        if period_date is None:
            issues.append(
                Issue(LEVEL_ERROR, "report_period", "报告期无法解析，该期入「待补」", code)
            )
            continue
        period = period_date.isoformat()
        report_type = item.get("report_type") or report_type_of(period)
        item["report_period"] = period
        item["report_type"] = report_type
        item["is_cumulative"] = report_type != "annual"
        if is_missing(item.get("effective_date")):
            item["effective_date"] = _effective_date_of(period)

        key = (period, str(report_type))
        existing = kept.get(key)
        if existing is not None:
            issues.append(
                Issue(
                    LEVEL_WARN,
                    "report_period",
                    f"重复报告期 {period}/{report_type}，保留最新一条",
                    code,
                )
            )
            if str(existing.get("fetched_at") or "") >= str(item.get("fetched_at") or ""):
                continue
        kept[key] = item

    ordered = sorted(kept.values(), key=lambda item: (item["report_period"], item["report_type"]))

    for item in ordered:
        effective = _to_date(item.get("effective_date"))
        if effective is not None and effective > today:
            message = (
                f"{item['report_period']} 尚未生效（生效日 {item['effective_date']}），"
                "不参与本期评分"
            )
            issues.append(Issue(LEVEL_WARN, "effective_date", message, code))
        balance = check_balance(item)
        if balance is not None:
            issues.append(balance)

    for field_name in WATCH_FIELDS:
        issues.extend(detect_changes(ordered, field_name=field_name))

    annual = [item for item in ordered if item["report_type"] == "annual"]
    interim = [item for item in ordered if item["report_type"] != "annual"]
    window = annual[-config.YEARS :]
    if interim:
        window = sorted([*window, interim[-1]], key=lambda item: item["report_period"])
    return window, issues


# ── 行情 ──────────────────────────────────────────────────────────────────
def merge_price_rows(
    code: str,
    raw_rows: list[dict[str, Any]],
    qfq_rows: list[dict[str, Any]],
    *,
    decimals: int = config.ADJ_FACTOR_DECIMALS,
) -> tuple[list[dict[str, Any]], list[Issue]]:
    """把「不复权」与「前复权」两次取数按交易日合并，并反算 ``adj_factor``（家法 R8）。

    单次取数只给一种口径（见 :mod:`..datasource.akshare_src` 的边界说明），
    标准层要求 ``close`` 与 ``close_qfq`` 同时存在，故在此合并。
    ``adj_factor = close_qfq / close``（前复权价 / 原始价），保留 ``decimals`` 位。
    """
    issues: list[Issue] = []
    by_date: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        trade_date = str(row.get("trade_date") or "")[:10]
        if not trade_date:
            continue
        by_date[trade_date] = {**row, "code": row.get("code") or code, "close": row.get("close")}

    for row in qfq_rows:
        trade_date = str(row.get("trade_date") or "")[:10]
        if not trade_date:
            continue
        target = by_date.get(trade_date)
        if target is None:
            issues.append(
                Issue(
                    LEVEL_WARN,
                    "trade_date",
                    f"前复权行情 {trade_date} 在原始价序列中不存在，已忽略",
                    code,
                )
            )
            continue
        target["close_qfq"] = row.get("close_qfq")

    merged: list[dict[str, Any]] = []
    for trade_date in sorted(by_date):
        item = by_date[trade_date]
        close = item.get("close")
        close_qfq = item.get("close_qfq")
        if is_missing(close_qfq):
            issues.append(Issue(LEVEL_ERROR, "close_qfq", f"{trade_date} 缺前复权收盘价", code))
            continue
        if is_missing(close):
            issues.append(Issue(LEVEL_ERROR, "close", f"{trade_date} 缺原始收盘价", code))
            continue
        if float(close) == 0:
            item["adj_factor"] = None
            issues.append(
                Issue(LEVEL_WARN, "close", f"{trade_date} 原始价为 0，无法反算复权因子", code)
            )
        else:
            item["adj_factor"] = round(float(close_qfq) / float(close), decimals)
        merged.append(item)
    return merged, issues


# ── 覆盖度与总入口 ────────────────────────────────────────────────────────
def market_missing_rate(total: int, missing: int) -> float:
    """单市场缺失率（《数据源接入规格》§9）。"""
    if total <= 0:
        return 0.0
    return missing / total


def clean_universe(
    stocks: list[dict[str, Any]],
    financials: dict[str, list[dict[str, Any]]] | None = None,
    prices: dict[str, list[dict[str, Any]]] | None = None,
    *,
    today: date | None = None,
) -> CleanResult:
    """清洗整个样本（M1-4 总入口；M1-6 编排调用）。

    :param stocks: 标准层股票主数据。
    :param financials: ``{code: 财报行列表}``。
    :param prices: ``{code: 行情行列表}``（已合并口径的完整行）。
    """
    today = today or _today()
    result = CleanResult()
    known: set[str] = set()

    for row in stocks:
        cleaned, issues = normalize_stock(row, today=today)
        result.issues.extend(issues)
        code = cleaned.get("code")
        if any(issue.level == LEVEL_ERROR for issue in issues):
            result.pending.append(str(code))
            continue
        known.add(str(code))
        result.stocks.append(cleaned)

    for code, rows in (financials or {}).items():
        if code not in known:
            result.rejected.append(code)
            result.issues.append(
                Issue(LEVEL_ERROR, "code", f"财报外键校验失败：{code} 不在股票主数据中", code)
            )
            continue
        cleaned, issues = normalize_financial_rows(code, rows, today=today)
        result.issues.extend(issues)
        if cleaned:
            result.financials[code] = cleaned

    for code, rows in (prices or {}).items():
        if code not in known:
            result.rejected.append(code)
            result.issues.append(
                Issue(LEVEL_ERROR, "code", f"行情外键校验失败：{code} 不在股票主数据中", code)
            )
            continue
        result.prices[code] = rows

    for market in config.MARKETS:
        market_codes = [code for code in known if market_of(code) == market]
        if not market_codes:
            result.coverage[market] = 0.0
            continue
        missing = sum(1 for code in market_codes if not result.financials.get(code))
        result.coverage[market] = market_missing_rate(len(market_codes), missing)

    result.degraded = any(
        rate > config.MARKET_MISSING_RATE_MAX for rate in result.coverage.values()
    )

    for issue in result.issues:
        if issue.level == LEVEL_ERROR:
            logger.error("清洗异常 [%s] %s: %s", issue.code, issue.field, issue.message)
        else:
            logger.warning("清洗告警 [%s] %s: %s", issue.code, issue.field, issue.message)
    if result.degraded:
        logger.error("覆盖度超阈值 → 标记 degraded：%s", result.coverage)

    return result


__all__ = [
    "FINANCIAL_REQUIRED",
    "LEVEL_ERROR",
    "LEVEL_WARN",
    "STOCK_REQUIRED",
    "STATUS_VALUES",
    "WATCH_FIELDS",
    "CleanResult",
    "Issue",
    "check_balance",
    "clean_universe",
    "detect_changes",
    "market_missing_rate",
    "merge_price_rows",
    "normalize_financial_rows",
    "normalize_stock",
]
