"""AkShare 数据源实现（任务 M1-2；依据《数据源接入规格》§3 字段映射、§7 容错）。

要点（计划 M1-2）：
- **重试 3 次 + 指数退避 1s / 3s / 9s + 超时 15s**（参数取自 ``config``，禁止硬编码）；
- **本地文件缓存**：原始返回按日期落盘，可追溯、可复现、断网可用；
- **失败抛 ``DataSourceError``**（带 ``source`` / ``code``），由上层决定回退
  （回退链：新浪主源 → 同花顺 THS → 官方源 → ``fixtures/``，家法 R16）。

设计取舍：
- ``akshare`` **延迟导入**（``self.ak``），保证未装该库时本模块仍可 import、测试可离线跑；
- 调用注入点 ``ak`` / ``sleep`` / ``today`` 可替换，离线单测无需网络、无需真等待；
- 超时用「单线程池 + ``future.result(timeout)``」统一施加（akshare 各接口不一定接受
  ``timeout`` 参数）；超时后线程仍在后台跑完，但调用方立刻返回并进入重试。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
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

logger = logging.getLogger(__name__)

# ── AkShare 接口名（**以实际安装版本为准**，见《数据源接入规格》§3）────────
# 未联网逐一验证（记为《计划完成情况》D-8）；改版本时只需动这里。
ENDPOINTS: dict[str, str] = {
    "stock_list_a": "stock_info_a_code_name",  # A股 代码/名称
    "stock_list_hk": "stock_hk_spot_em",  # 港股 实时列表（含代码/名称）
    # 全市场当日快照（新浪系，D-32）：1 次调用覆盖全市场 —— 实测 A 股 18s/5575 行、港股 37s/2815 行
    "spot_a": "stock_zh_a_spot",
    "spot_hk": "stock_hk_spot",
    "price_a": "stock_zh_a_hist",  # A股 日线（支持 adjust）
    "price_hk": "stock_hk_hist",  # 港股 日线
    # A股 财报三表 —— **主源 = 新浪三表**（D-45 于「THS 主源」之后回调；16:53 复测三表 ≈5.8s/只，
    # 限速态 43~44s/张 见 D-38，由 D-42 请求节流护栏）。传参 stock=带前缀小写、symbol=报表名；
    # 列名跨行业变体按「关键词组」处理（D-31①，见 04 §6）。
    "financial_a": "stock_financial_report_sina",
    # A股 财报三表 —— **第 2 级源 = 同花顺 THS**（D-45；仅新浪失败时兜底，见 `AkShareThsSource`）：
    # 键名 `financial_{slug}_ths` 与 `_REPORT_SLUG` 对齐；传参 symbol=纯 6 位代码、
    # indicator="按年度"。归母权益 / 归母净利润**直给**（绕开 D-31① 金融股列名变体）。
    "financial_income_ths": "stock_financial_benefit_ths",  # 利润表
    "financial_balance_ths": "stock_financial_debt_ths",  # 资产负债表
    "financial_cashflow_ths": "stock_financial_cash_ths",  # 现金流量表
    # A 股行业：**申万口径**（本项目 industry_std = 申万2021）——
    # 2026-10-15 联网实测：东财板块接口（stock_board_industry_name_em / _cons_em /
    # stock_individual_info_em）全部 RemoteDisconnected；申万系接口稳定且快
    # （一级 1.9s / 二级 1.9s / 成分股 0.3s×131），故改走申万（见 D-8）。
    "industry_sw_first": "sw_index_first_info",  # 申万一级行业列表（31 个）
    "industry_sw": "sw_index_second_info",  # 申万二级行业列表（131 个，含「上级行业」）
    "industry_sw_cons": "index_component_sw",  # 申万行业成分股（symbol=行业代码）
    "industry_a": "stock_board_industry_name_em",  # 东财行业板块（保留备选，当前实测不可用）
    # 港股东财行业（ADR-011 降级路径）；同为东财系，实测不可用
    "industry_hk": "stock_hk_industry_name_em",
}

# 内部复权模式 → AkShare 的 adjust 参数值：``"raw"``（不复权）在 akshare 里是**空串**。
# 不映射会被 akshare 当非法参数拒绝，"不复权 + 前复权"两次取数里的前者必然失败 →
# 清洗阶段合并不出 ``adj_factor`` → 产物永远没有 kline（2026-10-15 实跑踩到，见 D-8）。
_ADJUST_TO_AK: dict[str, str] = {"qfq": "qfq", "hfq": "hfq", "raw": ""}

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
# 「单股批量接口」（三表 / 行情）**不在上面的名单里**：它们实测远超默认 15s（2026-10-15
# 新浪三表 ~20-45s，见 D-8；2026-10-16 正常档 新浪 ≈2s/张、THS ≈2.5s/张，限速态 43~44s/张，
# 见 D-38 / D-45），但也不该套用 1800s 的「全量翻页」上限 —— 目标站点挂起时会白等半小时。
# 改用 `config.FETCH_TIMEOUT_MEDIUM_SECONDS`（300s）在 `financials` / `price_daily` 里显式传参。

# 三表名称：财报三表在合并 dict 与缓存 slug 中的键（THS 接口键名亦按 slug 拼接）
REPORT_INCOME = "利润表"
REPORT_BALANCE = "资产负债表"
REPORT_CASHFLOW = "现金流量表"
# 缓存文件名用 ASCII 短名，避免中文文件名跨平台出问题；
# 财报缓存键 **含源名**（D-44）：`financial_{源}_{code}_{slug}`（源 = sina / ths，两源共存）
_REPORT_SLUG = {REPORT_INCOME: "income", REPORT_BALANCE: "balance", REPORT_CASHFLOW: "cashflow"}

# ── 通用工具（单位 / 空值 / 代码语义 / 表格归一）──────────────────────────
# 实现已移至 leaf 模块 `src/common.py`，本模块从文件头 re-export（见那里的说明）。


class AkShareSource:
    """AkShare 主源（M1-2）。

    :param cache_dir: 原始返回缓存根目录；默认 ``data/raw/akshare/``。
    :param partition: 缓存分区：``None`` = 按日分区 ``<日期>/``（时点数据：spot /
        股票列表 / 申万映射）；指定名字（如 ``"cold"``）= 固定子目录。注意：
        **全历史数据（财报三表 / 行情）一律落固定子目录 ``cold/``**（D-37），不受本参数影响。
    :param stale_after: 全历史数据的新鲜度 TTL（D-37）：``None`` = 永不过期（cold 基线）；
        设为时长（如 ``timedelta(days=20)``）时，缓存文件 mtime 超龄 → 视为未命中、
        重抓并覆盖写回（mtime 刷新）。动机：全市场跑批 67~202h 必然跨天，
        「同日缓存」判新鲜会误杀续传 —— TTL 与跑批时长解耦。
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
        partition: str | None = None,
        stale_after: timedelta | None = None,
        sleep: Callable[[float], None] | None = None,
        interval_sleep: Callable[[float], None] | None = None,
        ak: Any | None = None,
        today: Callable[[], date] | None = None,
        retry_times: int | None = None,
        backoff: tuple[int, ...] | None = None,
        timeout: float | None = None,
        slow_timeout: float | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir else config.DATA_DIR / "raw" / "akshare"
        self._partition = partition
        self._stale_after = stale_after
        self._sleep = sleep or time.sleep
        # 请求节流（D-42）：与退避 sleep 分开注入，互不干扰测试与语义
        self._interval_sleep = interval_sleep or time.sleep
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
    def _invoke(
        self, fn: Callable[..., Any], *, timeout: float | None = None, **kwargs: Any
    ) -> Any:
        """施加统一超时（akshare 各接口不一定接受 timeout 参数）。

        超时按**接口名**区分：``SLOW_ENDPOINTS``（全量列表类）用 ``self._slow_timeout``，
        其余用 ``self._timeout``；``timeout`` 显式传入时覆盖两者（申万成分等"应快但偶发抖动"
        的批量接口用中等超时，见 ``config.INDUSTRY_CONS_TIMEOUT_SECONDS``）。
        """
        if timeout is None:
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
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """带重试 / 退避 / 超时的调用；最终失败抛 ``DataSourceError``。

        退避序列取 ``config.FETCH_RETRY_BACKOFF``（3 次重试 → 2 次等待：1s、3s）。
        """
        last: Exception | None = None
        for attempt in range(1, self._retry_times + 1):
            try:
                return self._invoke(fn, timeout=timeout, **kwargs)
            except Exception as exc:  # akshare 抛出的异常类型跨版本不稳定，统一兜住
                last = exc
                if attempt < self._retry_times:
                    self._sleep(self._backoff[attempt - 1])
        raise DataSourceError(
            f"{what} 失败（已重试 {self._retry_times} 次）：{type(last).__name__}: {last}",
            source=self.name,
            code=code,
        ) from last

    # ── 本地缓存（D-37：全历史数据固定分区 + mtime TTL；时点数据按日分区）──
    # 财报键含源名（D-44：financial_sina_* / financial_ths_*，两源缓存共存）；前缀不变
    _HISTORY_PREFIXES = ("financial_", "price_")  # 全历史数据键前缀
    HISTORY_DIR_NAME = "cold"  # 固定分区名（与 cold 模式分区同名：同一份基线）

    def _is_history_key(self, key: str) -> bool:
        return key.startswith(self._HISTORY_PREFIXES)

    def _history_dir(self) -> Path:
        return self._cache_dir / self.HISTORY_DIR_NAME

    def _cache_path(self, key: str) -> Path:
        if self._is_history_key(key):
            return self._history_dir() / f"{key}.json"
        if key.startswith("spot_"):
            # 快照是时点数据：任何模式都取当日（否则扩池重跑会命中旧快照、污染行情截止日）
            return self._cache_dir / self._today().isoformat() / f"{key}.json"
        if self._partition:
            return self._cache_dir / self._partition / f"{key}.json"
        return self._cache_dir / self._today().isoformat() / f"{key}.json"

    def _is_fresh(self, path: Path) -> bool:
        """全历史数据新鲜度（D-37）：文件 mtime 距今 ≤ ``stale_after`` 视为新鲜。

        mtime 即最近一次抓取时间（写缓存自然刷新，覆盖写回即续期）；``stale_after=None``
        （cold 基线）永不过期。mtime 只做差值比较，与 UTC 时区无关。
        """
        if self._stale_after is None:
            return True
        fetched_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return datetime.now(UTC) - fetched_at <= self._stale_after

    def _load_cache(self, key: str) -> list[dict[str, Any]] | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        if self._is_history_key(key) and not self._is_fresh(path):
            logger.info("缓存超龄（>%s），视为未命中：%s", self._stale_after, path.name)
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
            # default=str 兜底：万一归一漏了非 JSON 原生类型（如某些版的日期对象），
            # 宁可写成字符串也不要让整批调用因写缓存而失败（D-8 实跑踩过）
            json.dump(rows, fh, ensure_ascii=False, default=str)

    def _fetch(
        self,
        key: str,
        fn: Callable[..., Any],
        *,
        what: str,
        code: str | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """缓存优先：命中直接返回；未命中则抓取、归一、落盘。"""
        cached = self._load_cache(key)
        if cached is not None:
            return cached
        self._throttle()
        rows = to_records(self._call(fn, what=what, code=code, timeout=timeout, **kwargs))
        self._save_cache(key, rows)
        return rows

    def _throttle(self) -> None:
        """请求节流（D-42）：真发请求前随机 sleep，避开上游站点按 IP 的速率限制。

        依据：AkShare 不内置限流；上游阈值（社区实测）新浪单 IP 45~65 次/60 秒、
        东财约 3 次/秒、同花顺 ``hexin-v`` 每值 5~10 次，超阈值触发 5~15 分钟阶梯式
        封禁（表现为每请求被压到 43~44s / 149s 的等间隔惩罚）。2~3 秒抖动 → 20~30
        请求/60 秒，稳在阈值内。**只对真发请求生效**（缓存命中在调用方已提前返回）。
        """
        low, high = config.FETCH_INTERVAL_JITTER_SECONDS
        self._interval_sleep(random.uniform(low, high))

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
        stocks = [self._to_stock(row, market, now) for row in rows]
        if market == "A":
            # 行业回填（申万一级/二级）；映射缺失的个股保持「未分类」，不参与行业分位。
            # 行业映射**整体失败不阻断列表**（宁可用「未分类」也不要拉整条采集链回退）。
            try:
                _, mapping = self._sw_industry()
            except DataSourceError as exc:
                logger.warning("申万行业映射不可用，A 股行业按「未分类」处理：%s", exc)
                mapping = {}
            for stock in stocks:
                info = mapping.get(str(stock.get("code")))
                if info is None:
                    continue
                l1_code, l1_name, l2_code, l2_name = info
                stock.update(
                    {
                        "l1_code": l1_code,
                        "l1_name": l1_name,
                        "l2_code": l2_code,
                        "l2_name": l2_name,
                        "is_financial": l1_name in config.FINANCIAL_SECTORS,
                    }
                )
        return stocks

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
        """A 股财报三表 → 标准层行（**新浪三表·主源**，D-45；同花顺 THS 为链上第 2 级源）。

        传参：``stock`` = 带市场前缀的小写代码（``sh600188``）、``symbol`` = 报表名；
        解析与映射见 :func:`map_sina_financials`（列名跨行业变体按「关键词组」处理，D-31①）。
        缓存键 ``financial_sina_{code}_{slug}``（D-44 含源名）、落 ``cold/`` 固定分区（D-37）。

        港股不在本接口覆盖内 → 明确抛错（回退链切官方源 / 披露易，行为不变）。
        """
        if market_of(code) != "A":
            raise DataSourceError(
                f"港股财报接口尚未接入（{code}）："
                "AkShare 财报（新浪/THS）均只覆盖 A 股，港股走官方源兜底",
                source=self.name,
                code=code,
            )
        tables = {
            report: self._fetch(
                f"financial_sina_{code}_{_REPORT_SLUG[report]}",
                getattr(self.ak, ENDPOINTS["financial_a"]),
                what=f"财报（{code} / {report}）",
                code=code,
                timeout=config.FETCH_TIMEOUT_MEDIUM_SECONDS,  # 单张 ≈2s（限速态 44s），见 D-38
                stock=code.lower(),
                symbol=report,
            )
            for report in (REPORT_INCOME, REPORT_BALANCE, REPORT_CASHFLOW)
        }
        return map_sina_financials(code, tables)

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
            timeout=config.FETCH_TIMEOUT_MEDIUM_SECONDS,  # 单次实测 ~22s，见 D-8
            symbol=code if market == "HK" else code[2:],
            period="daily",
            adjust=_ADJUST_TO_AK.get(adjust, adjust),
        )
        return [self._to_price(row, code, adjust) for row in rows]

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
        if market not in config.MARKETS:
            raise ValueError(f"不支持的市场：{market!r}（应为 {config.MARKETS} 之一）")
        endpoint = ENDPOINTS["spot_a" if market == "A" else "spot_hk"]
        rows = self._fetch(
            f"spot_{market}",
            getattr(self.ak, endpoint),
            what=f"全市场快照（{market}）",
            timeout=config.FETCH_TIMEOUT_MEDIUM_SECONDS,  # 实测 18-37s，见 D-32
        )
        name_key = "名称" if market == "A" else "中文名称"
        snapshot: list[dict[str, Any]] = []
        for row in rows:
            code = _snapshot_code(row.get("代码"), market)
            if not code:
                continue
            snapshot.append(
                {
                    "code": code,
                    "name": str(pick(row, name_key) or "").replace(" ", ""),
                    "latest_price": _to_float(pick(row, "最新价")),
                    "open": _to_float(pick(row, "今开")),
                    "high": _to_float(pick(row, "最高")),
                    "low": _to_float(pick(row, "最低")),
                    "prev_close": _to_float(pick(row, "昨收")),
                    "volume": _to_float(pick(row, "成交量")),
                }
            )
        return snapshot

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

    def _sw_industry(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str, str, str]]]:
        """A 股行业：申万一级/二级节点 + 「股票 → (l1_code, l1_name, l2_code, l2_name)」映射。

        数据来源：``sw_index_first_info``（31 个一级）/ ``sw_index_second_info``（131 个二级，
        自带「上级行业」）/ ``index_component_sw``（成分股，symbol=行业代码）。全部按日缓存
        （首跑约 1 分钟；命中缓存后瞬时）。
        """
        std = config.INDUSTRY_STANDARD["A"]
        firsts = self._fetch(
            "sw_l1",
            getattr(self.ak, ENDPOINTS["industry_sw_first"]),
            what="申万一级行业列表（A）",
        )
        seconds = self._fetch(
            "sw_l2",
            getattr(self.ak, ENDPOINTS["industry_sw"]),
            what="申万二级行业列表（A）",
        )

        nodes: list[dict[str, Any]] = []
        l1_code_by_name: dict[str, str] = {}
        for row in firsts:
            code = str(pick(row, "行业代码", "code") or "").split(".")[0]
            name = str(pick(row, "行业名称", "name") or "").strip()
            if not name:
                continue
            l1_code_by_name[name] = code
            nodes.append(
                {
                    "standard": std,
                    "version": "v1",
                    "level": 1,
                    "node_code": code,
                    "node_name": name,
                    "parent_code": None,
                    "member_count": pick(row, "成份个数", "member_count"),
                }
            )

        mapping: dict[str, tuple[str, str, str, str]] = {}
        for row in seconds:
            l2_code = str(pick(row, "行业代码", "code") or "").split(".")[0]
            l2_name = str(pick(row, "行业名称", "name") or "").strip()
            l1_name = str(pick(row, "上级行业", "parent") or "").strip()
            if not l2_name:
                continue
            l1_code = l1_code_by_name.get(l1_name, "")
            nodes.append(
                {
                    "standard": std,
                    "version": "v1",
                    "level": 2,
                    "node_code": l2_code,
                    "node_name": l2_name,
                    "parent_code": l1_code,
                    "member_count": pick(row, "成份个数", "member_count"),
                }
            )
            if not self._cache_path(f"sw_cons_{l2_code}").exists():
                # 降流：连续 131 次请求会被目标站点限流（2026-10-15 实测，见 D-8）
                self._sleep(config.INDUSTRY_CONS_INTERVAL_SECONDS)
            try:
                members = self._fetch(
                    f"sw_cons_{l2_code}",
                    getattr(self.ak, ENDPOINTS["industry_sw_cons"]),
                    what=f"申万行业成分（{l2_name}）",
                    timeout=config.INDUSTRY_CONS_TIMEOUT_SECONDS,
                    symbol=l2_code,
                )
            except DataSourceError as exc:
                logger.warning("申万行业成分获取失败，跳过该行业（%s）：%s", l2_name, exc)
                continue
            for member in members:
                code = normalize_code(pick(member, "证券代码", "code"), "A")
                if code:
                    mapping[code] = (l1_code, l1_name, l2_code, l2_name)
        return nodes, mapping

    def industry_tree(self, market: str) -> list[dict[str, Any]]:
        if market not in config.MARKETS:
            raise ValueError(f"不支持的市场：{market!r}（应为 {config.MARKETS} 之一）")
        if market == "A":
            nodes, _ = self._sw_industry()  # A 股 = 申万一级 + 二级（见 ENDPOINTS 说明）
            return nodes
        # 港股：东财口径（ADR-011 降级路径）；东财接口不可用时由上层回退官方源 / fixtures
        endpoint = ENDPOINTS["industry_hk"]
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


class AkShareThsSource(AkShareSource):
    """同花顺 THS 财报**第 2 级源**（D-45）：只实现 ``financials``（A 股），其余接口一律抛错。

    链序（``fetch.default_chain``）：AkShareSource（新浪，主源）→ **本对象（THS）** →
    OfficialSource → FixturesSource。仅当新浪失败（限速 / 无数据）时兜底；
    缓存键 ``financial_ths_{code}_{slug}``（D-44 含源名，与新浪缓存 `financial_sina_*` 共存）。
    """

    name = "ths"

    def financials(self, code: str) -> list[dict[str, Any]]:
        """A 股财报三表 → 标准层行（同花顺 THS；第 2 级回退，D-45）。

        传参（《数据源接入规格》§3.2.1）：``symbol`` 为**纯 6 位代码**（不带 SH/SZ 前缀）、
        ``indicator="按年度"``；解析与映射见 :func:`map_ths_financials`。
        缓存键 ``financial_ths_{code}_{slug}``、落 ``cold/`` 固定分区（D-37）。

        港股不在 THS 覆盖内 → 明确抛错（让链继续到官方源）。
        """
        if market_of(code) != "A":
            raise DataSourceError(
                f"THS 仅覆盖 A 股（{code}）：港股财报走官方源兜底",
                source=self.name,
                code=code,
            )
        tables = {
            report: self._fetch(
                f"financial_ths_{code}_{_REPORT_SLUG[report]}",
                getattr(self.ak, ENDPOINTS[f"financial_{_REPORT_SLUG[report]}_ths"]),
                what=f"财报（{code} / {report}）",
                code=code,
                timeout=config.FETCH_TIMEOUT_MEDIUM_SECONDS,  # 单只三表 ≈8s（D-39 / D-45 复测）
                symbol=code[2:],  # THS 要纯 6 位代码（不带市场前缀）
                indicator="按年度",
            )
            for report in (REPORT_INCOME, REPORT_BALANCE, REPORT_CASHFLOW)
        }
        return map_ths_financials(code, tables)

    # ── 其余接口：本对象只承担 THS 财报二级回退，一律抛错让链继续 ──────────
    def list_stocks(self, market: str) -> list[dict[str, Any]]:
        raise DataSourceError(
            f"THS 二级源不提供股票列表（market={market}）：只在新浪失败时兜底财报",
            source=self.name,
        )

    def price_daily(self, code: str, adjust: str = config.DEFAULT_ADJUST) -> list[dict[str, Any]]:
        raise DataSourceError(f"THS 二级源不提供日行情（{code}）", source=self.name, code=code)

    def industry_tree(self, market: str) -> list[dict[str, Any]]:
        raise DataSourceError(f"THS 二级源不提供行业分类树（market={market}）", source=self.name)

    def market_snapshot(self, market: str) -> list[dict[str, Any]]:
        raise DataSourceError(f"THS 二级源不提供全市场快照（market={market}）", source=self.name)


# ── 全市场快照（D-32）：列名/代码归一 ────────────────────────────────────
def _to_float(value: Any) -> float | None:
    """快照列 → float（字符串 / 千分位 / NaN / ``--`` 一律 ``None``）。"""
    if value is None:
        return None
    try:
        text = str(value).replace(",", "").strip()
        return float(text) if text and text.lower() not in {"nan", "--"} else None
    except ValueError:
        return None


def _snapshot_code(raw: Any, market: str) -> str | None:
    """新浪快照的代码 → 标准层格式（``SH600519`` / ``BJ920000`` / ``HK00700``）。

    A 股形如 ``sh600519``（小写带前缀）；港股形如 ``00001``（5 位数字）。
    无法识别返回 ``None``（调用方跳过该行）。
    """
    text = str(raw or "").strip().lower()
    if market == "HK":
        digits = "".join(ch for ch in text if ch.isdigit())
        return f"HK{int(digits):05d}" if digits else None
    prefix = text[:2] if text[:2] in {"sh", "sz", "bj"} else ""
    digits = text[2:] if prefix else text
    if prefix and digits.isdigit() and len(digits) == 6:
        return f"{prefix.upper()}{digits}"
    return None


# ── 新浪三表合并 → 标准层 financial_report（**主源**，D-45 恢复；《数据字典》§3.2）──
# 科目列名按语义给候选：新浪源跨版本列名会变，逐个试（改映射只动这里）。
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


# 关键词组（**优先于**上面的精确候选）：组内关键词**全部出现**即命中，按行内列序扫描。
# 只用在「高优先语义唯一」的科目上（前缀「归属于母公司」），否则会误命中子项
# （如「流动负债合计」之于负债合计）。跨行业列名变体见《计划完成情况》D-31① ——
# 实测三种写法一个都不在精确候选里：
#   工商银行「归属于母公司股东的权益」（**无「合计」**）/ 中国平安「归属于母公司的股东权益合计」/
#   贵州茅台「归属于母公司股东权益合计」；此前 equity 因此在金融股上为 None、
#   在普通股上回落到**含少数股东权益的合并口径**（口径与《数据字典》§3.2 的「归母」不符）。
_SUBJECT_KEYWORDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "net_profit": (("归属于母公司", "净利润"), ("归属母公司", "净利润")),
    "equity": (("归属于母公司", "权益"), ("归属母公司", "权益")),
}


def _subject(row: dict[str, Any], key: str) -> Any:
    """按科目取原始值：**关键词组优先**（抗跨源 / 跨行业列名变体），精确候选兜底（D-31①）。"""
    for group in _SUBJECT_KEYWORDS.get(key, ()):
        for column, cell in row.items():
            if not is_missing(cell) and all(word in str(column) for word in group):
                return cell
    return pick(row, *_SUBJECTS[key])


def map_sina_financials(
    code: str,
    tables: dict[str, list[dict[str, Any]]],
    *,
    source: str = "akshare",
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    """新浪三表（行 = 报告期，列 = 科目）→ 标准层 ``financial_report`` 行（**主源**，D-45）。

    :param tables: ``{报表名: 行列表}``，报表名为 ``REPORT_INCOME`` / ``REPORT_BALANCE`` /
        ``REPORT_CASHFLOW``；同一报告期的三表会按报告期合并为一行。
    :returns: 按报告期升序的标准层行（字段见 ``base.FINANCIAL_REQUIRED_FIELDS``）。

    口径（家法 R7 / R9 / R10 / R11）：
    - 单位：元 → 亿元、保留 2 位小数；空值 ``None``，**不用 0 顶替**；
    - ``gross_profit`` 缺列时用 ``revenue − cost`` 反算（《数据字典》§3.2 注）；
    - ``effective_date`` = 报告期 + 4 个月；非年报标 ``is_cumulative=True``
      （累计口径不可比，不进主评分）；
    - 行内 ``source`` 默认 ``'akshare'``（= 新浪三表，经 AkShare；04 §3.2 来源标识沿用）；
      第 2 级源 THS 的映射见 :func:`map_ths_financials`。
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


# ── THS 三表解析 → 标准层 financial_report（规格 §3.2.1；D-39 实测，D-45 起为第 2 级源）──
# 实测（2026-10-16，9 只样本 × 全部历史年份）：本路径取用的金额列，取值恒为**带单位
# 字符串**（'4529.44亿' / '53.48万亿' / '7262.80万'），缺失恒为 ``False``；金融股
# 部分列**整列不存在**（工行无 `总现金`/`货币资金`/`其中：营业成本`，平安、中信无
# `其中：营业成本`）→ 按家法「缺失一律 null」处理，不另找列顶替。
# 列名**逐字**取自 §3.2.1（带 `*` 前缀与全角冒号），改映射只动这里。
_THS_COLUMNS: dict[str, tuple[str, ...]] = {
    "revenue": ("其中：营业收入", "*营业总收入"),  # 缺失回退 *营业总收入
    "cost": ("其中：营业成本",),  # 金融股无此列 → 毛利率随之缺失
    "net_profit": ("*归属于母公司所有者的净利润",),  # 归母口径直给
    "total_assets": ("*资产合计",),
    "total_liabilities": ("*负债合计",),
    "equity": ("*归属于母公司所有者权益合计",),  # 归母口径直给（绕开 D-31①）
    "op_cashflow": ("*经营活动产生的现金流量净额",),
    "icf": ("*投资活动产生的现金流量净额",),
    "fcf": ("*筹资活动产生的现金流量净额",),
}
# 报告期语义（report_period_of / report_type_of / effective_date_of）同样在
# `src/common.py`，本模块 re-export。

# 单位档（§3.2.1：「单位含 万亿/亿/万/元 多档」；万亿 须先于 万/亿 匹配 —— 正则交替有序）
_THS_UNIT_SCALE: dict[str, float] = {"万亿": 1e4, "亿": 1.0, "万": 1e-4, "元": 1e-8}
_THS_AMOUNT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)(万亿|亿|万|元)$")
# 视同缺失的字符串形态（缓存 JSON 往返可能把 False 落成字符串、'--' 等防御式兜底）
_THS_MISSING_STRINGS = frozenset({"", "false", "none", "null", "nan", "--", "-"})


def _ths_missing_cell(value: Any) -> bool:
    """THS 单元格是否缺失：``False``（实测的缺失标记）/ ``None`` / NaN / 空串形态。"""
    if value is None or value is False:
        return True
    if isinstance(value, str) and value.strip().lower() in _THS_MISSING_STRINGS:
        return True
    return is_missing(value)


def parse_ths_amount(value: Any) -> float | None:
    """THS 金额 → **亿元**（2 位小数）；缺失 / 非金额格式 → ``None``（家法 R11，不臆造）。

    - ``'4529.44亿'`` → ``4529.44``；``'53.48万亿'`` → ``534800.0``；
      ``'7262.80万'`` → ``0.73``；``'1234元'`` → ``0.0``
    - ``False``（实测缺失标记）/ ``None`` / 空串 → ``None``
    - 比值（``'23.38%'``）等非金额格式 → ``None``；**非字符串**（数值型）同样 → ``None``
      —— THS 金额恒为带单位字符串（实测 2600+ 个取值零例外），无单位数字不作推断。
    """
    if _ths_missing_cell(value) or not isinstance(value, str):
        return None
    text = value.strip().replace(",", "").replace("－", "-").replace("−", "-")
    match = _THS_AMOUNT_RE.match(text)
    if match is None:
        return None
    return round(float(match.group(1)) * _THS_UNIT_SCALE[match.group(2)], config.AMOUNT_DECIMALS)


def _ths_value(row: dict[str, Any], *columns: str) -> Any:
    """按列名顺序取第一个**非缺失**的原始值；列不存在 / ``False`` / 空 → 继续往后试。"""
    for column in columns:
        if column in row and not _ths_missing_cell(row[column]):
            return row[column]
    return None


def _ths_amount(row: dict[str, Any], *columns: str) -> float | None:
    """取第一个可用列并解析为亿元（列缺失 / 值缺失 / 非金额格式 → ``None``）。"""
    return parse_ths_amount(_ths_value(row, *columns))


def map_ths_financials(
    code: str,
    tables: dict[str, list[dict[str, Any]]],
    *,
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    """THS 三表（行 = 年度报告期，列 = 带单位字符串）→ 标准层 ``financial_report`` 行。

    :param tables: ``{报表名: 行列表}``，报表名为 ``REPORT_INCOME`` / ``REPORT_BALANCE`` /
        ``REPORT_CASHFLOW``；同一报告期的三表按报告期合并为一行。
    :returns: 按报告期升序的标准层行（字段见 ``base.FINANCIAL_REQUIRED_FIELDS``）。

    口径（《数据源接入规格》§3.2.1 逐条落地；家法 R7 / R9 / R10 / R11）：
    - **只取近 ``config.YEARS`` 个完整财年**（三接口各返回 23~31 年，窗口裁剪口径同 §4）；
    - 金额「亿元」2 位小数（`parse_ths_amount`），缺失 ``None``，**不用 0 顶替**；
    - ``gross_profit`` = 营业收入 − 营业成本（金融股无营业成本 → ``None``）；
    - ``cash`` 三级回退：资产负债表 ``总现金`` → ``货币资金`` → 现金流表
      ``*期末现金及现金等价物余额``（银行股前两列整列不存在，由第三级兜底）；
    - ``report_type='annual'`` / ``is_cumulative=False`` / ``currency='CNY'`` /
      ``source='ths'`` 固定（``indicator="按年度"``；THS 仅覆盖 A 股，均为人民币报表）。
    """
    by_period: dict[str, dict[str, dict[str, Any]]] = {}
    for report, rows in tables.items():
        for row in rows:
            period = report_period_of(row)
            if period is not None:
                by_period.setdefault(period, {})[report] = row

    stamp = fetched_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[dict[str, Any]] = []
    for period in sorted(by_period)[-config.YEARS :]:
        income = by_period[period].get(REPORT_INCOME, {})
        balance = by_period[period].get(REPORT_BALANCE, {})
        cashflow = by_period[period].get(REPORT_CASHFLOW, {})

        revenue = _ths_amount(income, *_THS_COLUMNS["revenue"])
        cost = _ths_amount(income, *_THS_COLUMNS["cost"])
        gross = (
            round(revenue - cost, config.AMOUNT_DECIMALS)
            if revenue is not None and cost is not None
            else None
        )
        cash = None
        for source_row, column in (
            (balance, "总现金"),
            (balance, "货币资金"),
            (cashflow, "*期末现金及现金等价物余额"),
        ):
            cash = _ths_amount(source_row, column)
            if cash is not None:
                break

        out.append(
            {
                "code": code,
                "report_period": period,
                "report_type": "annual",
                "is_cumulative": False,
                "revenue": revenue,
                "gross_profit": gross,
                "net_profit": _ths_amount(income, *_THS_COLUMNS["net_profit"]),
                "total_assets": _ths_amount(balance, *_THS_COLUMNS["total_assets"]),
                "total_liabilities": _ths_amount(balance, *_THS_COLUMNS["total_liabilities"]),
                "equity": _ths_amount(balance, *_THS_COLUMNS["equity"]),
                "cash": cash,
                "op_cashflow": _ths_amount(cashflow, *_THS_COLUMNS["op_cashflow"]),
                "icf": _ths_amount(cashflow, *_THS_COLUMNS["icf"]),
                "fcf": _ths_amount(cashflow, *_THS_COLUMNS["fcf"]),
                "effective_date": effective_date_of(period),
                "currency": "CNY",
                "source": "ths",
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
    "AkShareThsSource",
    "effective_date_of",
    "exchange_of",
    "is_missing",
    "map_sina_financials",
    "map_ths_financials",
    "market_of",
    "normalize_code",
    "parse_ths_amount",
    "pick",
    "report_period_of",
    "report_type_of",
    "status_of",
    "to_internal_billion",
    "to_records",
]
