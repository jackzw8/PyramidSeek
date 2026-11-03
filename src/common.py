"""跨层通用工具（**leaf 模块**：除 ``config`` 与异常类型外不依赖包内任何模块）。

为什么单独成模块（《计划完成情况》D-12）：这些函数与"用哪个数据源"**无关** ——
单位换算、空值判定、证券代码语义、报告期语义、候选列取值 —— 但原先寄居在
``datasource/akshare_src.py`` 里，导致 ``official_src``、``report_parser``、
``pipeline/clean.py`` 都要从"某个数据源的实现模块"导入通用工具，**依赖方向被读歪**。

约定：
- 本模块只做**纯函数**，不做 IO、不联网、不依赖 pandas（保持轻量、可离线 import）；
- ``akshare_src`` 仍 re-export 这些名字（既有 import 不破）；**新代码请直接
  ``from ..common import ...``**。
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import config
from .datasource.base import DataSourceError

# 交易所映射（《数据字典》§3.1）
_EXCHANGE_BY_PREFIX = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE", "HK": "HKEX"}

# 报告期列候选名（数据源列名跨版本会变，故按语义逐个试）
_PERIOD_KEYS = ("报告日", "报告期", "报表日期", "截止日期", "date")
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


# ── 单位与空值（家法 R10 / R11）───────────────────────────────────────────
def is_missing(value: Any) -> bool:
    """判定空值：``None`` / ``NaN`` / ``NaT``。

    用「自身不等于自身」判 NaN/NaT，避免在模块层引入 pandas（保持轻量、可离线 import）。
    """
    if value is None:
        return True
    try:
        return bool(value != value)  # NaN / NaT 是唯一「自身不等于自身」的值
    except (TypeError, ValueError):
        return False


def to_internal_billion(value: Any) -> float | None:
    """元 → 亿元，保留 2 位小数；缺失返回 ``None``（**不用 0 顶替**）。

    舍入走 :func:`round_half_up`（家法：禁内置 ``round``，见 PL-08/EN-02）。
    """
    if is_missing(value):
        return None
    return round_half_up(float(value) / 1e8, config.AMOUNT_DECIMALS)


# ── 证券代码语义 ──────────────────────────────────────────────────────────
def _guess_prefix(digits: str) -> str:
    """A股交易所前缀推断（仅用于数据源未给前缀时）。"""
    if digits.startswith(("6", "9")):
        return "SH"
    if digits.startswith(("0", "2", "3")):
        return "SZ"
    if digits.startswith(("4", "8")):
        return "BJ"
    raise DataSourceError(f"无法推断 A股交易所前缀：{digits!r}")


def normalize_code(raw: Any, market: str) -> str:
    """代码归一为「市场前缀 + 数字」，如 ``600900`` → ``SH600900``、``700`` → ``HK00700``。

    已有前缀（``SH`` / ``SZ`` / ``BJ`` / ``HK``）则只做补零。
    """
    text = str(raw).strip().upper()
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        raise DataSourceError(f"无法解析证券代码：{raw!r}")
    if market == "HK":
        return "HK" + digits.zfill(5)
    prefix = text[:2] if text[:2] in ("SH", "SZ", "BJ") else _guess_prefix(digits)
    return prefix + digits.zfill(6)


def exchange_of(code: str) -> str:
    return _EXCHANGE_BY_PREFIX[code[:2]]


def market_of(code: str) -> str:
    return "HK" if code.startswith("HK") else "A"


def status_of(name: str) -> str:
    """由证券简称判定状态（《数据字典》§3.1：normal / ST / suspended / delisted / new）。

    这里只能判定名称可见的部分：``*ST`` / ``ST`` → ``ST``，含「退」→ ``delisted``；
    ``suspended`` 与 ``new``（次新）需行情与上市日期，由清洗阶段补齐。
    """
    text = (name or "").replace(" ", "")
    if "退" in text:
        return "delisted"
    if "ST" in text.upper():
        return "ST"
    return "normal"


def round_half_up(value: float, decimals: int) -> float:
    """半进位舍入，**与 JS 的 ``Math.round(x*10^d)/10^d`` / ``toFixed`` 逐位一致**。

    **不要用内置 ``round``**：它是「银行家舍入」（``.5`` 取偶），边界上会差一个单位。
    反例：``round(3.125, 2) == 3.12``（Python）而 ``Math.round(312.5)/100 == 3.13``（JS）——
    分位分 ``(0 + 0.5×1)/16×100 = 3.125`` 正好落在这种边界上（16 只股票的同业池，最低分就是它）。

    **注意方向**：JS 的 ``Math.round`` / ``toFixed`` 遇 ``.5`` 一律**朝 ``+∞``** 取整，
    对负数是"向零"（``Math.round(-2.5) === -2``，不是 ``-3``）。这里刻意保留这个**不对称**行为 ——
    "更好看"的对称舍入反而会让 M2-6 双引擎逐项比对失败（《计划完成情况》D-18）。
    """
    factor = 10**decimals
    return math.floor(value * factor + 0.5) / factor


# ── 表格归一 ──────────────────────────────────────────────────────────────
def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """一行归一：空值 → ``None``；**日期 / 时间 → ISO 字符串**（保证可 JSON 序列化）。

    日期归一很关键：``akshare`` 不同接口的日期列有的给 ``str``、有的给 ``datetime.date`` /
    ``pandas.Timestamp``（如 ``index_component_sw`` 的「计入日期」）—— 后者直接 ``json.dump``
    会抛 ``TypeError``，让「写缓存」把整批调用带崩（2026-10-15 实跑踩过，见 D-8）。
    """
    out: dict[str, Any] = {}
    for key, val in row.items():
        if is_missing(val):
            out[str(key)] = None
        elif isinstance(val, date):  # datetime / pandas.Timestamp 均为 date 子类
            out[str(key)] = val.isoformat()
        else:
            out[str(key)] = val
    return out


def to_records(table: Any) -> list[dict[str, Any]]:
    """pandas DataFrame（或已归一 ``list[dict]``）→ ``list[dict]``，空值统一为 ``None``。"""
    if table is None:
        return []
    if hasattr(table, "to_dict"):
        rows = table.to_dict("records")
    elif isinstance(table, list):
        rows = table
    else:
        raise DataSourceError(f"无法识别的返回类型：{type(table)!r}")
    return [_normalize_row(row) for row in rows]


def pick(row: dict[str, Any], *names: str) -> Any:
    """按候选列名取值（数据源列名跨版本会变，故按语义逐个试）。"""
    for name in names:
        if name in row:
            return row[name]
    return None


# ── 报告期语义 ────────────────────────────────────────────────────────────
def report_period_of(row: dict[str, Any]) -> str | None:
    """取一行里的报告期并归一为 ``YYYY-MM-DD``；取不到返回 ``None``。

    数字串**取前 8 位**即可，因为数据源三种形态都出现在同一列上：``"20241231"``、
    ``"2024-12-31"``、``"2024-12-31 00:00:00"``（``datetime`` / ``Timestamp`` 的 ``str``，
    共 14 位数字）。早先只认「恰好 8 位」，第三种直接返回 ``None``，导致该报告期**整行
    被静默丢弃**、表现为「数据源没数据」（DS-03）。

    取出的年月日做一次合法性校验：像 ``31/12/2024`` 这种日/月顺序不同的写法，硬取前 8 位
    会得到 ``3112-20-24`` 的垃圾值 —— 宁可返回 ``None``（家法 R11：不臆造）。
    """
    raw = pick(row, *_PERIOD_KEYS)
    if raw is None:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) >= 8:
        year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        if not (1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
            return None
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 4:
        return f"{digits}-12-31"
    return None


def report_type_of(report_period: str) -> str:
    """按报告月份归一到 ``annual`` / ``h1`` / ``q1`` / ``q3``。"""
    month = int(report_period[5:7])
    return {12: "annual", 6: "h1", 3: "q1", 9: "q3"}.get(month, "q1")


def _last_day(year: int, month: int) -> int:
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        return 29
    return _DAYS_IN_MONTH[month - 1]


def effective_date_of(report_period: str, lag_months: int = config.EFFECTIVE_LAG_MONTHS) -> str:
    """报告期 + 4 个月生效，并截断到该月最后一天（家法 R9）。

    例：``2024-12-31`` + 4 → ``2025-04-30``（4 月没有 31 日）。
    """
    year, month, day = (int(part) for part in report_period.split("-"))
    month += lag_months
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return f"{year:04d}-{month:02d}-{min(day, _last_day(year, month)):02d}"


# ── 披露日历（D-52）──────────────────────────────────────────────────────
def beijing_today(now: datetime | None = None) -> date:
    """北京日期（UTC+8，无夏令时）。

    披露窗口与「应披露报告期」的判定都必须用它 —— 用 UTC 日期会在北京时间 00:00~08:00
    之间错判一整天（例：11-06 07:00 北京 = 11-05 23:00 UTC，按 UTC 算会误落在三季报窗口内）。
    """
    moment = now or datetime.now(UTC)
    return (moment + timedelta(hours=8)).date()


def disclosure_window(today: date) -> str | None:
    """今天落在哪个披露季窗口（返回窗口名）；淡季返回 ``None``。

    窗口表见 ``config.DISCLOSURE_WINDOWS``（含 5 天缓冲，端点日也算窗口内）。
    """
    month_day = (today.month, today.day)
    for name, start, end in config.DISCLOSURE_WINDOWS:
        if start <= month_day <= end:
            return name
    return None


def financials_ttl_days(today: date) -> int:
    """财报缓存 TTL（天）：披露季短、淡季长（D-52）。"""
    if disclosure_window(today):
        return config.FINANCIALS_FRESH_DAYS
    return config.FINANCIALS_FRESH_DAYS_OFF_SEASON


def expected_report_period(today: date) -> str:
    """当前时点「最新可能已披露」的报告期（预筛判定依据）。

    预筛用（D-52）：缓存里的最新报告期**已追平或超过**它 → 这只票不必重抓。

    切换点是**各季披露开始日**，不是截止日 —— 报表是提前陆续披露的，若按「截止日 + 缓冲」
    切换，10 月整月（三季报陆续发布）都会被判成「只需半年报」，等于整季漏更新。

    ====================  ==========================
    日期区间              应到报告期
    ====================  ==========================
    1/1  ~ 1/24           上年 09-30（年报季未启动）
    1/25 ~ 3/31           上年 12-31（年报陆续披露）
    4/1  ~ 6/30           本年 03-31（一季报陆续披露）
    7/1  ~ 9/30           本年 06-30（半年报陆续披露）
    10/1 ~ 12/31          本年 09-30（三季报陆续披露）
    ====================  ==========================
    """
    year = today.year
    month_day = (today.month, today.day)
    if month_day >= (10, 1):
        return f"{year}-09-30"
    if month_day >= (7, 1):
        return f"{year}-06-30"
    if month_day >= (4, 1):
        return f"{year}-03-31"
    if month_day >= (1, 25):
        return f"{year - 1}-12-31"
    return f"{year - 1}-09-30"


__all__ = [
    "beijing_today",
    "disclosure_window",
    "effective_date_of",
    "exchange_of",
    "expected_report_period",
    "financials_ttl_days",
    "is_missing",
    "market_of",
    "normalize_code",
    "pick",
    "report_period_of",
    "report_type_of",
    "round_half_up",
    "status_of",
    "to_internal_billion",
    "to_records",
]
