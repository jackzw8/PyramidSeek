"""AkShare 数据源实现（任务 M1-2；依据《数据源接入规格》§3 字段映射、§7 容错）。

要点（计划 M1-2）：
- **重试 3 次 + 指数退避 1s / 3s / 9s + 超时 15s**（参数取自 ``config``，禁止硬编码）；
- **本地文件缓存**：原始返回按日期落盘，可追溯、可复现、断网可用；
- **失败抛 ``DataSourceError``**（带 ``source`` / ``code``），由上层决定回退
  （主源 → 官方源 → ``fixtures/``，家法 R16）。

设计取舍：
- ``akshare`` **延迟导入**（``self.ak``），保证未装该库时本模块仍可 import、测试可离线跑；
- 调用注入点 ``ak`` / ``sleep`` / ``today`` 可替换，离线单测无需网络、无需真等待；
- 超时用「单线程池 + ``future.result(timeout)``」统一施加（akshare 各接口不一定接受
  ``timeout`` 参数）；超时后线程仍在后台跑完，但调用方立刻返回并进入重试。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .. import config

# 通用工具（单位 / 空值 / 代码语义 / 报告期语义 / 表格归一）已于 2026-10-14 提取到
# leaf 模块 `src/common.py`（见《计划完成情况》D-12）。此处 **re-export** 它们，
# 保证既有 `from .akshare_src import is_missing, ...` 不破；**新代码请直接 import common**。
from ..common import (
    effective_date_of,
    exchange_of,
    is_missing,
    market_of,
    normalize_code,
    pick,
    report_period_of,
    report_type_of,
    status_of,
    to_internal_billion,
    to_records,
)
from .base import DataSourceError

# ── AkShare 接口名（**以实际安装版本为准**，见《数据源接入规格》§3）────────
# 未联网逐一验证（记为《计划完成情况》D-8）；改版本时只需动这里。
ENDPOINTS: dict[str, str] = {
    "stock_list_a": "stock_info_a_code_name",  # A股 代码/名称
    "stock_list_hk": "stock_hk_spot_em",  # 港股 实时列表（含代码/名称）
    "price_a": "stock_zh_a_hist",  # A股 日线（支持 adjust）
    "price_hk": "stock_hk_hist",  # 港股 日线
    "financial_a": "stock_financial_report_sina",  # A股 三表（symbol=报表名）
    "industry_a": "stock_board_industry_name_em",  # A股 东财行业板块（→ 申万映射表见 §6）
    "industry_hk": "stock_hk_industry_name_em",  # 港股 东财行业（ADR-011 降级路径）
}

# 全量列表类接口单次调用耗时很长（**2026-10-14 实测**：``stock_info_a_code_name`` 取 A股
# 5574 只约 13 分钟），不能用默认 15s 超时——15s 是《数据源接入规格》§7 对**单次请求**的
# 约束，不适用于这种内部翻页的批量接口。
SLOW_ENDPOINTS: frozenset[str] = frozenset(
    {
        "stock_info_a_code_name",
        "stock_hk_spot_em",
        "stock_board_industry_name_em",
        "stock_hk_industry_name_em",
    }
)

# 三表名称（akshare financial_a 的 symbol 参数）
REPORT_INCOME = "利润表"
REPORT_BALANCE = "资产负债表"
REPORT_CASHFLOW = "现金流量表"
# 缓存文件名用 ASCII 短名，避免中文文件名跨平台出问题
_REPORT_SLUG = {REPORT_INCOME: "income", REPORT_BALANCE: "balance", REPORT_CASHFLOW: "cashflow"}

# ── 通用工具（单位 / 空值 / 代码语义 / 表格归一）──────────────────────────
# 实现已移至 leaf 模块 `src/common.py`，本模块从文件头 re-export（见那里的说明）。


class AkShareSource:
    """AkShare 主源（M1-2）。

    :param cache_dir: 原始返回缓存根目录；默认 ``data/raw/akshare/<日期>/``。
    :param sleep: 退避用的睡眠函数（离线测试注入，避免真等待）。
    :param ak: akshare 模块（离线测试注入假模块；``None`` 时延迟导入真库）。
    :param today: 返回当前日期的函数（测试注入，控制缓存分区）。
    :param timeout: 普通接口超时（秒），默认 ``config.FETCH_TIMEOUT_SECONDS``。
    :param slow_timeout: ``SLOW_ENDPOINTS`` 超时（秒），默认
        ``config.FETCH_TIMEOUT_SLOW_SECONDS``。
    """

    name = "akshare"

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        sleep: Callable[[float], None] | None = None,
        ak: Any | None = None,
        today: Callable[[], date] | None = None,
        retry_times: int | None = None,
        backoff: tuple[int, ...] | None = None,
        timeout: float | None = None,
        slow_timeout: float | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir else config.DATA_DIR / "raw" / "akshare"
        self._sleep = sleep or time.sleep
        self._ak = ak
        self._today = today or (lambda: datetime.now(UTC).date())
        self._retry_times = retry_times if retry_times is not None else config.FETCH_RETRY_TIMES
        self._backoff = backoff if backoff is not None else config.FETCH_RETRY_BACKOFF
        self._timeout = timeout if timeout is not None else config.FETCH_TIMEOUT_SECONDS
        self._slow_timeout = (
            slow_timeout if slow_timeout is not None else config.FETCH_TIMEOUT_SLOW_SECONDS
        )

    # ── 延迟导入 akshare：未安装不影响 import 本模块 ──────────────────────
    @property
    def ak(self) -> Any:
        if self._ak is None:
            try:
                import akshare  # noqa: PLC0415  （刻意延迟导入）
            except ImportError as exc:
                raise DataSourceError(
                    "akshare 未安装：pip install akshare", source=self.name
                ) from exc
            self._ak = akshare
        return self._ak

    # ── 超时 ──────────────────────────────────────────────────────────────
    def _invoke(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        """施加统一超时（akshare 各接口不一定接受 timeout 参数）。

        超时按**接口名**区分：``SLOW_ENDPOINTS``（全量列表类）用 ``self._slow_timeout``，
        其余用 ``self._timeout``。
        """
        is_slow = getattr(fn, "__name__", "") in SLOW_ENDPOINTS
        timeout = self._slow_timeout if is_slow else self._timeout
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(fn, **kwargs).result(timeout=timeout)
        finally:
            pool.shutdown(wait=False)

    # ── 重试 + 退避 ───────────────────────────────────────────────────────
    def _call(
        self,
        fn: Callable[..., Any],
        *,
        what: str,
        code: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """带重试 / 退避 / 超时的调用；最终失败抛 ``DataSourceError``。

        退避序列取 ``config.FETCH_RETRY_BACKOFF``（3 次重试 → 2 次等待：1s、3s）。
        """
        last: Exception | None = None
        for attempt in range(1, self._retry_times + 1):
            try:
                return self._invoke(fn, **kwargs)
            except Exception as exc:  # akshare 抛出的异常类型跨版本不稳定，统一兜住
                last = exc
                if attempt < self._retry_times:
                    self._sleep(self._backoff[attempt - 1])
        raise DataSourceError(
            f"{what} 失败（已重试 {self._retry_times} 次）：{type(last).__name__}: {last}",
            source=self.name,
            code=code,
        ) from last

    # ── 本地缓存（原始返回按日期落盘）─────────────────────────────────────
    def _cache_path(self, key: str) -> Path:
        return self._cache_dir / self._today().isoformat() / f"{key}.json"

    def _load_cache(self, key: str) -> list[dict[str, Any]] | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, list) else None

    def _save_cache(self, key: str, rows: list[dict[str, Any]]) -> None:
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(rows, fh, ensure_ascii=False)

    def _fetch(
        self,
        key: str,
        fn: Callable[..., Any],
        *,
        what: str,
        code: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """缓存优先：命中直接返回；未命中则抓取、归一、落盘。"""
        cached = self._load_cache(key)
        if cached is not None:
            return cached
        rows = to_records(self._call(fn, what=what, code=code, **kwargs))
        self._save_cache(key, rows)
        return rows

    # ── DataSource 接口实现（对应《数据源接入规格》§2）────────────────────
    def list_stocks(self, market: str) -> list[dict[str, Any]]:
        if market not in config.MARKETS:
            raise ValueError(f"不支持的市场：{market!r}（应为 {config.MARKETS} 之一）")
        endpoint = ENDPOINTS["stock_list_a" if market == "A" else "stock_list_hk"]
        rows = self._fetch(
            f"list_{market}",
            getattr(self.ak, endpoint),
            what=f"股票列表（{market}）",
        )
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return [self._to_stock(row, market, now) for row in rows]

    @staticmethod
    def _to_stock(row: dict[str, Any], market: str, updated_at: str) -> dict[str, Any]:
        name = str(pick(row, "name", "名称", "简称") or "").strip()
        code = normalize_code(pick(row, "code", "代码", "symbol"), market)
        # 行业归属按《数据源接入规格》§6：缺失映射 → 归入「未分类」，暂不参与行业分位
        return {
            "code": code,
            "name": name,
            "market": market,
            "exchange": exchange_of(code),
            "l1_code": None,
            "l1_name": config.INDUSTRY_UNCLASSIFIED,
            "l2_code": None,
            "l2_name": config.INDUSTRY_UNCLASSIFIED,
            "industry_std": config.INDUSTRY_STANDARD[market],
            "listed_at": None,  # 需单独接口，M1-4 清洗阶段补齐
            "shares": to_internal_billion(pick(row, "总股本", "shares")),
            "status": status_of(name),
            "is_financial": False,  # 依赖行业映射表判定（§6），M1-6 编排阶段补齐
            "updated_at": updated_at,
        }

    def financials(self, code: str) -> list[dict[str, Any]]:
        if market_of(code) != "A":
            raise DataSourceError(
                f"港股财报接口尚未接入（{code}）：M1-2 先覆盖 A股，港股走官方源兜底",
                source=self.name,
                code=code,
            )
        tables = {
            report: self._fetch(
                f"financial_{code}_{_REPORT_SLUG[report]}",
                getattr(self.ak, ENDPOINTS["financial_a"]),
                what=f"财报（{code} / {report}）",
                code=code,
                stock=code.lower(),
                symbol=report,
            )
            for report in (REPORT_INCOME, REPORT_BALANCE, REPORT_CASHFLOW)
        }
        return map_financial_reports(code, tables)

    def price_daily(self, code: str, adjust: str = config.DEFAULT_ADJUST) -> list[dict[str, Any]]:
        if adjust not in config.ADJUST_MODES:
            raise ValueError(f"不支持的复权方式：{adjust!r}（应为 {config.ADJUST_MODES} 之一）")
        market = market_of(code)
        endpoint = ENDPOINTS["price_a" if market == "A" else "price_hk"]
        rows = self._fetch(
            f"price_{code}_{adjust}",
            getattr(self.ak, endpoint),
            what=f"日行情（{code} / {adjust}）",
            code=code,
            symbol=code if market == "HK" else code[2:],
            period="daily",
            adjust=adjust,
        )
        return [self._to_price(row, code, adjust) for row in rows]

    @staticmethod
    def _to_price(row: dict[str, Any], code: str, adjust: str) -> dict[str, Any]:
        """归一单日行情。

        口径（《数据字典》§3.3）：``close`` = **不复权**原始价、``close_qfq`` = **前复权**价。
        单次 AkShare 调用只返回所请求的那一种口径，故这里只填与 ``adjust`` 对应的列，
        另一列留 ``None`` —— **两次取数的合并与 ``adj_factor`` 反算归 M1-4（清洗标准化）**。
        这样做的目的是不让"前复权价"被下游误当成"原始价"。
        """
        close = pick(row, "收盘", "close")
        trade_date = pick(row, "日期", "date")
        return {
            "code": code,
            "trade_date": str(trade_date)[:10] if trade_date is not None else None,
            "open": pick(row, "开盘", "open"),
            "high": pick(row, "最高", "high"),
            "low": pick(row, "最低", "low"),
            "close": close if adjust == "raw" else None,
            "close_qfq": close if adjust == config.DEFAULT_ADJUST else None,
            "adj_factor": None,
            "volume": pick(row, "成交量", "volume"),
        }

    def industry_tree(self, market: str) -> list[dict[str, Any]]:
        if market not in config.MARKETS:
            raise ValueError(f"不支持的市场：{market!r}（应为 {config.MARKETS} 之一）")
        endpoint = ENDPOINTS["industry_a" if market == "A" else "industry_hk"]
        std = config.INDUSTRY_STANDARD[market]
        if market == "HK":
            # ADR-011 降级路径：AkShare 不提供 GICS，暂用东财口径跑通全链路
            std = config.INDUSTRY_STANDARD_HK_FALLBACK
        rows = self._fetch(
            f"industry_{market}",
            getattr(self.ak, endpoint),
            what=f"行业分类（{market}）",
        )
        return [
            {
                "standard": std,
                "version": "v1",
                "level": 1,  # 东财板块列表只给一级，二级树待自建映射表
                "node_code": str(pick(row, "板块代码", "代码", "code") or ""),
                "node_name": str(pick(row, "板块名称", "名称", "name") or ""),
                "parent_code": None,
                "member_count": pick(row, "公司家数", "member_count"),
            }
            for row in rows
        ]

    def health_check(self) -> bool:
        """连通性自检：只试 1 次、不重试、不落缓存，失败返回 ``False``。"""
        try:
            self._invoke(getattr(self.ak, ENDPOINTS["stock_list_a"]))
        except Exception:
            return False
        return True


# ── 三表合并 → 标准层 financial_report（《数据字典》§3.2）──────────────────
# 科目列名按语义给候选：AkShare / 新浪源跨版本列名会变，逐个试（改映射只动这里）。
_SUBJECTS: dict[str, tuple[str, ...]] = {
    "revenue": ("营业总收入", "营业收入"),
    "gross_profit": ("营业毛利", "毛利"),
    "cost": ("营业总成本", "营业成本"),
    "net_profit": ("归属于母公司所有者的净利润", "归属母公司股东的净利润", "净利润"),
    "total_assets": ("资产总计", "资产合计"),
    "total_liabilities": ("负债合计",),
    "equity": ("归属于母公司所有者权益合计", "所有者权益(或股东权益)合计", "股东权益合计"),
    "cash": ("货币资金",),
    "op_cashflow": ("经营活动产生的现金流量净额",),
    "icf": ("投资活动产生的现金流量净额",),
    "fcf": ("筹资活动产生的现金流量净额",),
}
# 报告期语义（report_period_of / report_type_of / effective_date_of）同样在
# `src/common.py`，本模块 re-export。


def _subject(row: dict[str, Any], key: str) -> Any:
    return pick(row, *_SUBJECTS[key])


def map_financial_reports(
    code: str,
    tables: dict[str, list[dict[str, Any]]],
    *,
    source: str = "akshare",
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    """三表（行 = 报告期，列 = 科目）→ 标准层 ``financial_report`` 行。

    :param tables: ``{报表名: 行列表}``，报表名为 ``REPORT_INCOME`` / ``REPORT_BALANCE`` /
        ``REPORT_CASHFLOW``；同一报告期的三表会按报告期合并为一行。
    :returns: 按报告期升序的标准层行（字段见 ``base.FINANCIAL_REQUIRED_FIELDS``）。

    口径（家法 R7 / R9 / R10 / R11）：
    - 单位：元 → 亿元、保留 2 位小数；空值 ``None``，**不用 0 顶替**；
    - ``gross_profit`` 缺列时用 ``revenue − cost`` 反算（《数据字典》§3.2 注）；
    - ``effective_date`` = 报告期 + 4 个月；非年报标 ``is_cumulative=True``
      （累计口径不可比，不进主评分）。
    """
    by_period: dict[str, dict[str, dict[str, Any]]] = {}
    for report, rows in tables.items():
        for row in rows:
            period = report_period_of(row)
            if period is not None:
                by_period.setdefault(period, {})[report] = row

    stamp = fetched_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[dict[str, Any]] = []
    for period in sorted(by_period):
        income = by_period[period].get(REPORT_INCOME, {})
        balance = by_period[period].get(REPORT_BALANCE, {})
        cashflow = by_period[period].get(REPORT_CASHFLOW, {})

        revenue = to_internal_billion(_subject(income, "revenue"))
        gross = to_internal_billion(_subject(income, "gross_profit"))
        cost = to_internal_billion(_subject(income, "cost"))
        if gross is None and revenue is not None and cost is not None:
            gross = round(revenue - cost, config.AMOUNT_DECIMALS)

        report_type = report_type_of(period)
        out.append(
            {
                "code": code,
                "report_period": period,
                "report_type": report_type,
                "is_cumulative": report_type != "annual",
                "revenue": revenue,
                "gross_profit": gross,
                "net_profit": to_internal_billion(_subject(income, "net_profit")),
                "total_assets": to_internal_billion(_subject(balance, "total_assets")),
                "total_liabilities": to_internal_billion(_subject(balance, "total_liabilities")),
                "equity": to_internal_billion(_subject(balance, "equity")),
                "cash": to_internal_billion(_subject(balance, "cash")),
                "op_cashflow": to_internal_billion(_subject(cashflow, "op_cashflow")),
                "icf": to_internal_billion(_subject(cashflow, "icf")),
                "fcf": to_internal_billion(_subject(cashflow, "fcf")),
                "effective_date": effective_date_of(period),
                "currency": "CNY",  # 新浪三表为人民币报表（D-24 加的字段）
                "source": source,
                "fetched_at": stamp,
            }
        )
    return out


__all__ = [
    "ENDPOINTS",
    "REPORT_BALANCE",
    "REPORT_CASHFLOW",
    "REPORT_INCOME",
    "AkShareSource",
    "effective_date_of",
    "exchange_of",
    "is_missing",
    "map_financial_reports",
    "market_of",
    "normalize_code",
    "pick",
    "report_period_of",
    "report_type_of",
    "status_of",
    "to_internal_billion",
    "to_records",
]
