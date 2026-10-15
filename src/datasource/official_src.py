"""官方源兜底实现（任务 M1-3）：巨潮资讯（A股）/ 港交所披露易（港股）。

定位（《数据源接入规格》§1、§8）：**兜底与对账**，不做全量高频抓取，按需触发。

M1-3 交付「解析骨架」，边界写清楚：
- ✅ 已实现：HTTP 客户端（重试 / 退避 / 超时 / **请求间隔**）、公告检索、年报定位、
  PDF 下载与缓存、`health_check`、主源失败时自动切换的 :func:`call_with_fallback`；
- ⛔ 未实现：PDF → 三表结构化解析（:meth:`OfficialSource.parse_annual_report`）。
  M1-3 阶段它**明确抛错并列出已定位到的年报**，绝不返回假数据 —— 上层据此再回退
  `fixtures/`（三级回退链：AkShare → 官方源 → fixtures）。

职责边界：官方披露源**只提供财报**，不提供股票列表 / 行业树 / 日行情，对应方法一律
抛 ``DataSourceError`` 并说明原因（让回退链知道"这一类数据它本来就没有"）。

设计取舍（与 :mod:`akshare_src` 一致）：``requests`` 延迟导入；``session`` / ``sleep`` /
``today`` / ``clock`` 均可注入 → **全部用例可离线跑、无需真等待**。
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, NamedTuple

from .. import config

# 通用工具取自 leaf 模块 src/common.py（2026-10-14 由 akshare_src 提取，见《计划完成情况》D-12）
from ..common import is_missing, market_of, pick
from . import report_parser
from .base import DataSourceError

# ── 端点（巨潮 2026-10-14 联网验证通过；披露易 2026-10-14 按实测写法修正，见 D-21）──
CNINFO_ANNOUNCEMENT_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_ORG_SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_STATIC_BASE = "http://static.cninfo.com.cn/"
CNINFO_ANNUAL_CATEGORY = "category_ndbg_szsh"  # 巨潮「年度报告」板块代码
# 检索接口的 column 参数按交易所取值（2026-10-14 联网验证）
CNINFO_COLUMNS = {"SH": "sse", "SZ": "szse", "BJ": "bj"}

HKEX_BASE = "https://www1.hkexnews.hk"
# ① 反查内部 ``stockId``：检索接口要的是它，**不是**股票代号本身（M1-3 时直接拿 `00700`
#    当 stockId 传，是错的 —— 见《计划完成情况》D-21）
HKEX_PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
# ② 年报检索：**返回 HTML 表格**而非 JSON（M1-3 的实现按 JSON 解析，必然失败）
#    t1code=40000「财务报表/ESG资料」、t2code=40100「年报」
HKEX_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml"
HKEX_T1_CODE = "40000"
HKEX_T2_CODE = "40100"


class FallbackResult(NamedTuple):
    """主源 → 官方源回退的结果（M1-6 编排据此写产物 ``degraded`` 标记）。"""

    data: list[dict[str, Any]]
    source: str  # 实际提供数据的源名
    degraded: bool  # 是否发生了回退
    reason: str | None  # 回退原因（主源的异常摘要）


def call_with_fallback(
    primary: Any,
    fallback: Any,
    method: str,
    *args: Any,
    **kwargs: Any,
) -> FallbackResult:
    """调用主源；主源抛异常时**自动改用官方源**（M1-3 验收要求）。

    :returns: :class:`FallbackResult`（含 ``degraded`` 与回退原因）。
    :raises DataSourceError: 官方源**也**失败时抛出它的异常，
        由上层继续回退到 ``fixtures/``（家法 R16）。
    """
    try:
        data = getattr(primary, method)(*args, **kwargs)
    except Exception as exc:  # 主源任何异常都视为可回退
        return FallbackResult(
            data=getattr(fallback, method)(*args, **kwargs),
            source=getattr(fallback, "name", "official"),
            degraded=True,
            reason=f"{type(exc).__name__}: {exc}",
        )
    return FallbackResult(
        data=data,
        source=getattr(primary, "name", "primary"),
        degraded=False,
        reason=None,
    )


def pdf_name_of(code: str, report_period: str) -> str:
    """PDF 缓存文件名（ASCII，避免中文标题进文件名）。"""
    return f"{code}_{report_period}.pdf"


# 非年报的标题标记（半年报 / 季报 / 摘要 / 英文版）——不能当成年报处理
NOT_ANNUAL_MARKERS = (
    "半年度",
    "半年报",
    "季度报告",
    "季报",
    "一季",
    "三季",
    "摘要",
    "英文",
)


def report_period_of_title(title: str) -> str | None:
    """从公告标题取**年报**年份 → 报告期 ``YYYY-12-31``；不是年报则返回 ``None``。

    ``"长江电力2024年年度报告"`` → ``"2024-12-31"``；``"…2025年半年度报告"`` → ``None``。

    **2026-10-14 真实数据验证发现**：``年度报告`` 是 ``半年度报告`` 的子串，
    只按"含年度报告 + 取前 4 位数字"会把半年报误判成 ``2025-12-31``
    （凭空造出一个不存在的财年）。故先按题型标记排除半年报 / 季报 / 摘要 / 英文版。
    """
    text = title or ""
    if any(marker in text for marker in NOT_ANNUAL_MARKERS):
        return None
    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if len(digits) >= 4:
        year = int(digits[:4])
        if 1990 <= year <= 2100:
            return f"{year}-12-31"
    return None


class OfficialSource:
    """官方源兜底（巨潮 / 披露易）。

    :param market: 自检与默认路由用的市场（``"A"`` / ``"HK"``）；实际按 **code** 路由。
    :param session: requests 会话（离线测试注入假会话；``None`` 时延迟导入并自建）。
    :param sleep: 睡眠函数（测试注入，避免真等待）。
    :param today: 返回当前日期的函数（控制缓存分区）。
    :param clock: 单调时钟（配合 ``min_interval`` 做请求间隔）。
    """

    name = "official"

    def __init__(
        self,
        market: str = "A",
        *,
        session: Any | None = None,
        sleep: Callable[[float], None] | None = None,
        today: Callable[[], date] | None = None,
        clock: Callable[[], float] | None = None,
        cache_dir: Path | None = None,
        partition: str | None = None,
        retry_times: int | None = None,
        backoff: tuple[int, ...] | None = None,
        timeout: float | None = None,
        min_interval: float | None = None,
        text_extractor: Any | None = None,
    ) -> None:
        self._market = market
        self._session = session
        self._extract_text = text_extractor or report_parser.extract_text
        self._sleep = sleep or time.sleep
        self._today = today or (lambda: datetime.now(UTC).date())
        self._clock = clock or time.monotonic
        self._cache_dir = Path(cache_dir) if cache_dir else config.DATA_DIR / "raw" / "official"
        self._partition = partition
        self._retry_times = retry_times if retry_times is not None else config.FETCH_RETRY_TIMES
        self._backoff = backoff if backoff is not None else config.FETCH_RETRY_BACKOFF
        self._timeout = timeout if timeout is not None else config.FETCH_TIMEOUT_SECONDS
        self._min_interval = (
            min_interval if min_interval is not None else config.FETCH_MIN_INTERVAL_SECONDS
        )
        self._last_request_at: float | None = None

    # ── 会话（延迟导入 requests）──────────────────────────────────────────
    @property
    def session(self) -> Any:
        if self._session is None:
            try:
                import requests  # noqa: PLC0415  （刻意延迟导入）
            except ImportError as exc:  # pragma: no cover - 由注入假会话的用例覆盖
                raise DataSourceError(
                    "requests 未安装：pip install requests", source=self.name
                ) from exc
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": config.HTTP_USER_AGENT})
        return self._session

    # ── 抓取频率（《数据源接入规格》§8：遵守目标站点抓取规范）─────────────
    def _throttle(self) -> None:
        """请求间隔节流：距上次请求不足 ``min_interval`` 时先睡够。

        用"每次请求前补足固定间隔"的保守做法（而非精确算 elapsed），
        保证任何情况下都不会高于目标站点可接受的频率。``min_interval <= 0`` 视为不限速。
        """
        if self._last_request_at is not None and self._min_interval > 0:
            self._sleep(self._min_interval)

    # ── 带重试 / 退避 / 超时的 HTTP ────────────────────────────────────────
    def _request(
        self, method: str, url: str, *, what: str, code: str | None = None, **kwargs: Any
    ) -> Any:
        last: Exception | None = None
        for attempt in range(1, self._retry_times + 1):
            self._throttle()
            self._last_request_at = self._clock()
            try:
                response = self.session.request(method, url, timeout=self._timeout, **kwargs)
                response.raise_for_status()
                return response
            except Exception as exc:  # 网络异常 / 4xx / 5xx 统一按可重试处理
                last = exc
                if attempt < self._retry_times:
                    self._sleep(self._backoff[attempt - 1])
        raise DataSourceError(
            f"{what} 失败（已重试 {self._retry_times} 次）：{type(last).__name__}: {last}",
            source=self.name,
            code=code,
        ) from last

    # ── 缓存（日常按日分区；cold 模式固定分区做跨天续传，D-34）────────────
    def _cache_dir_today(self) -> Path:
        if self._partition:
            return self._cache_dir / self._partition
        return self._cache_dir / self._today().isoformat()

    def _read_json_cache(self, key: str) -> Any | None:
        path = self._cache_dir_today() / f"{key}.json"
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_json_cache(self, key: str, payload: Any) -> None:
        path = self._cache_dir_today() / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False)

    def _download_pdf(self, url: str, *, code: str, report_period: str) -> Path:
        """下载年报 PDF 到当日缓存目录；已存在且非空则直接复用。"""
        path = self._cache_dir_today() / pdf_name_of(code, report_period)
        if path.exists() and path.stat().st_size > 0:
            return path
        response = self._request(
            "GET", url, what=f"年报 PDF 下载（{code} {report_period}）", code=code
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content or b"")
        return path

    # ── 公告检索 ──────────────────────────────────────────────────────────
    def _cninfo_org_id(self, code: str) -> str:
        """查巨潮内部 ``orgId``。

        **2026-10-14 真实数据验证**：检索接口的 ``stock`` 参数必须是 ``代码,orgId``；
        只给 ``600900`` 时接口返回 **HTTP 200 但 ``announcements`` 恒为 0 条**
        —— 这就是 D-8 / D-16 里「检索端点返回 0 条」的真正原因。
        """
        symbol = code[2:]
        response = self._request(
            "POST",
            CNINFO_ORG_SEARCH_URL,
            what=f"巨潮 orgId 查询（{code}）",
            code=code,
            data={"keyWord": symbol, "maxNum": 10},
        )
        payload = response.json()
        for item in payload if isinstance(payload, list) else []:
            if str(item.get("code")) == symbol and item.get("orgId"):
                return str(item["orgId"])
        raise DataSourceError(
            f"未查到巨潮 orgId（{code}）：标的可能不存在，或检索规则需更新",
            source=self.name,
            code=code,
        )

    def _cninfo_announcements(self, code: str) -> list[dict[str, Any]]:
        cached = self._read_json_cache(f"announcements_{code}")
        if cached is not None:
            return cached
        org_id = self._cninfo_org_id(code)
        response = self._request(
            "POST",
            CNINFO_ANNOUNCEMENT_URL,
            what=f"巨潮公告检索（{code}）",
            code=code,
            data={
                "pageNum": 1,
                "pageSize": 30,
                "column": CNINFO_COLUMNS.get(code[:2], "sse"),
                "tabName": "fulltext",
                "plate": "",
                "stock": f"{code[2:]},{org_id}",  # ← 关键：代码 + orgId
                "searchkey": "",
                "secid": "",
                "category": CNINFO_ANNUAL_CATEGORY,
                "trade": "",
                "seDate": "",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
        )
        rows = _as_records(response.json(), key="announcements")
        self._write_json_cache(f"announcements_{code}", rows)
        return rows

    def _hkex_stock_id(self, code: str) -> int:
        """查披露易内部 ``stockId``（整数）。

        **2026-10-14 修正**（依据《技术研究/年报获取方法》里实测跑通的写法）：检索接口要的
        ``stockId`` 是内部编号，**不是**股票代号 —— M1-3 直接拿 ``code[2:]`` 当 stockId 传，
        是 D-21 的两个根因之一。
        """
        symbol = code[2:]
        response = self._request(
            "GET",
            HKEX_PREFIX_URL,
            what=f"披露易 stockId 查询（{code}）",
            code=code,
            params={
                "callback": "callback",
                "lang": "ZH",
                "type": "A",
                "name": symbol,
                "market": "SEHK",
            },
        )
        match = re.search(r'"stockId"\s*:\s*(\d+)', response.text or "")
        if match is None:
            raise DataSourceError(
                f"未查到披露易 stockId（{code}）：标的可能不存在，或检索规则需更新",
                source=self.name,
                code=code,
            )
        return int(match.group(1))

    def _hkex_announcements(self, code: str, *, years: int) -> list[dict[str, Any]]:
        """检索年报公告（**返回 HTML 表格**，交给 :func:`_parse_hkex_rows` 解析）。"""
        cached = self._read_json_cache(f"announcements_{code}")
        if cached is not None:
            return cached
        stock_id = self._hkex_stock_id(code)
        # 财年 Y 的年报多在 Y+1 年披露（港股要求财年结束后 4 个月内），故窗口右端取到明年
        last_year = self._today().year
        first_year = last_year - max(years, 1) - 1
        response = self._request(
            "POST",
            HKEX_SEARCH_URL,
            what=f"披露易公告检索（{code}）",
            code=code,
            params={"lang": "zh"},
            data={
                "lang": "ZH",
                "category": "0",
                "market": "SEHK",
                "searchType": "1",
                "documentType": "-1",
                "t1code": HKEX_T1_CODE,
                "t2Gcode": "-2",
                "t2code": HKEX_T2_CODE,
                "stockId": str(stock_id),
                "from": f"{first_year}0101",
                "to": f"{last_year + 1}1231",
                "MB-Daterange": "0",
                "title": "",
            },
            headers={"Referer": f"{HKEX_SEARCH_URL}?lang=zh"},
        )
        rows = _parse_hkex_rows(response.text or "", code=code)
        self._write_json_cache(f"announcements_{code}", rows)
        return rows

    def annual_report_documents(self, code: str, years: int = config.YEARS) -> list[dict[str, Any]]:
        """定位近 ``years`` 年年报，并把 PDF **下载到本地缓存**。

        :returns: 按报告期倒序的列表，每项含 ``code`` / ``report_period`` / ``title`` /
            ``pdf_url`` / ``local_path``（字符串，便于序列化与断言）。
        """
        if market_of(code) == "HK":
            rows = self._hkex_announcements(code, years=years)
            url_of, title_of, period_of = _hkex_file_link, _hkex_title, hkex_report_period_of_title
        else:
            rows = self._cninfo_announcements(code)
            url_of, title_of, period_of = _cninfo_file_url, _cninfo_title, report_period_of_title

        candidates: list[dict[str, Any]] = []
        for row in rows:
            url = url_of(row)
            title = title_of(row)
            if not url or not title:
                continue
            period = period_of(title)
            if period is None:
                continue
            candidates.append(
                {
                    "code": code,
                    "report_period": period,
                    "title": title,
                    "pdf_url": url,
                    # 港股中英文是两套文件：同一年度优先中文版（解析器认中文科目名）
                    "is_english": str(row.get("lang") or "").lower() == "en",
                }
            )
        # 期次倒序；同一期次内中文版排前（``is_english=False`` 先）
        candidates.sort(key=lambda item: (-int(item["report_period"][:4]), item["is_english"]))

        # 先截断再下载：检索页动辄 30 条公告，全下会白拉几十份 PDF（真跑时才暴露的浪费）
        wanted: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            if item["report_period"] in seen:  # 同一期次只留一份（中文优先）
                continue
            seen.add(item["report_period"])
            wanted.append(item)
            if len(wanted) >= max(years, 0):
                break
        for item in wanted:
            item["local_path"] = str(
                self._download_pdf(item["pdf_url"], code=code, report_period=item["report_period"])
            )
        return wanted

    # ── 三表解析（D-11：年报 PDF → 标准层）────────────────────────────────
    def parse_annual_report(self, document: dict[str, Any]) -> list[dict[str, Any]]:
        """把已下载的年报 PDF 解析为标准层 ``financial_report`` 行（**本期 + 上期**）。

        解析逻辑在 :mod:`report_parser`（数据源层内部工具）；文本抽取可注入
        （构造参数 ``text_extractor``），便于离线测试与将来换 OCR。
        """
        code = document.get("code")
        local_path = document.get("local_path")
        if not local_path:
            raise DataSourceError(f"年报缺少本地文件（{code}）", source=self.name, code=code)
        text = self._extract_text(local_path)
        if not text.strip():
            raise DataSourceError(
                f"年报 PDF 抽不到文本（{local_path}）：可能是扫描件，需要 OCR",
                source=self.name,
                code=code,
            )
        return report_parser.build_report_rows(
            code,
            text,
            report_period=document["report_period"],
            source=self.name,
            fetched_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    # ── DataSource 接口 ───────────────────────────────────────────────────
    def list_stocks(self, market: str) -> list[dict[str, Any]]:
        raise DataSourceError(
            f"官方披露源不提供股票列表（market={market}）：它只用于财报兜底与对账",
            source=self.name,
        )

    def financials(self, code: str) -> list[dict[str, Any]]:
        documents = self.annual_report_documents(code)
        if not documents:
            raise DataSourceError(
                f"未检索到年报公告（{code}）：官方源无此标的，或检索规则需更新",
                source=self.name,
                code=code,
            )
        periods = "、".join(doc["report_period"] for doc in documents)
        rows: list[dict[str, Any]] = []
        try:
            for document in documents:
                rows.extend(self.parse_annual_report(document))
        except DataSourceError as exc:
            raise DataSourceError(
                f"{exc}（已定位 {len(documents)} 份年报：{periods}）",
                source=self.name,
                code=code,
            ) from exc
        return rows

    def price_daily(self, code: str, adjust: str = config.DEFAULT_ADJUST) -> list[dict[str, Any]]:
        raise DataSourceError(
            f"官方披露源不提供日行情（{code}）：行情兜底不在本期范围",
            source=self.name,
            code=code,
        )

    def industry_tree(self, market: str) -> list[dict[str, Any]]:
        raise DataSourceError(
            f"官方披露源不提供行业分类树（market={market}）",
            source=self.name,
        )

    def health_check(self) -> bool:
        """连通性自检：只发一次轻量 GET，失败返回 ``False``（不抛异常）。"""
        url = HKEX_SEARCH_URL if self._market == "HK" else CNINFO_STATIC_BASE
        try:
            self.session.request("GET", url, timeout=self._timeout)
        except Exception:
            return False
        return True


def _as_records(payload: Any, *, key: str) -> list[dict[str, Any]]:
    """从官方接口返回里取出记录列表（各接口结构不一致，防御式解析）。"""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    if isinstance(value, str):  # 披露易把结果塞在 JSON 字符串里
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(value, dict):
            value = value.get(key, [])
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _cninfo_title(row: dict[str, Any]) -> str:
    return str(pick(row, "announcementTitle", "title", "标题") or "")


def _cninfo_file_url(row: dict[str, Any]) -> str | None:
    adjunct = pick(row, "adjunctUrl", "adjuncturl", "url")
    if is_missing(adjunct):
        return None
    text = str(adjunct)
    return text if text.startswith("http") else CNINFO_STATIC_BASE + text


def hkex_report_period_of_title(title: str) -> str | None:
    """披露易标题 → 报告期（**只认年报**）；不是年报返回 ``None``。

    中文用「年報」而不是 A 股的「年度报告」；同一批返回里还混着中期报告、ESG 报告等，
    故与 :func:`report_period_of_title` 分开实现（《计划完成情况》D-21）。

    >>> hkex_report_period_of_title("年報 2023")
    '2023-12-31'
    >>> hkex_report_period_of_title("中期報告 2023") is None
    True
    """
    text = title or ""
    match = (
        re.search(r"(\d{4})\s*年報", text)
        or re.search(r"年報[^\d]{0,12}(\d{4})", text)
        or re.search(r"(\d{4})\s*ANNUAL\s*REPORT", text, re.I)
        or re.search(r"ANNUAL\s*REPORT[^\d]{0,12}(\d{4})", text, re.I)
    )
    if match is None:
        return None
    year = int(match.group(1))
    return f"{year}-12-31" if 1990 <= year <= 2100 else None


def _hkex_row_text(block: str) -> str:
    """从结果行 HTML 取「文件 / Document:」之后的描述文字（披露易把标题放那儿）。"""
    text = html.unescape(re.sub(r"<[^>]+>", " ", block))
    text = re.sub(r"\s+", " ", text).strip()
    for marker in ("文件:", "文件：", "Document:"):
        if marker in text:
            return text.split(marker, 1)[1].strip()
    return text


def _parse_hkex_rows(page: str, *, code: str = "") -> list[dict[str, Any]]:
    """把披露易检索结果页（**HTML 表格**）解析成 ``{code,title,url,lang}`` 行。

    该端点返回 HTML，M1-3 的实现按 JSON 解析（``response.json()``）必然失败 —— D-21 根因之二。
    ``lang`` 按文件名判定：中文版以 ``_c.pdf`` 结尾，英文版无后缀（早期为 ``_e.pdf``）。
    """
    rows: list[dict[str, Any]] = []
    for block in re.findall(r"<tr[^>]*>.*?</tr>", page or "", re.S | re.I):
        title = _hkex_row_text(block)
        for link in re.findall(r'href="([^"]+\.pdf)"', block, re.I):
            url = link if link.startswith("http") else f"{HKEX_BASE}/{link.lstrip('/')}"
            rows.append(
                {
                    "code": code,
                    "title": title,
                    "url": url,
                    "lang": "zh" if link.lower().endswith("_c.pdf") else "en",
                }
            )
    return rows


def _hkex_title(row: dict[str, Any]) -> str:
    return str(pick(row, "TITLE", "title", "标题") or "")


def _hkex_file_link(row: dict[str, Any]) -> str | None:
    link = pick(row, "FILE_LINK", "fileLink", "url", "URL")
    if is_missing(link):
        return None
    text = str(link)
    if text.startswith("http"):
        return text
    return HKEX_BASE + ("" if text.startswith("/") else "/") + text


__all__ = [
    "CNINFO_ANNOUNCEMENT_URL",
    "CNINFO_ANNUAL_CATEGORY",
    "CNINFO_STATIC_BASE",
    "HKEX_BASE",
    "HKEX_PREFIX_URL",
    "HKEX_SEARCH_URL",
    "FallbackResult",
    "OfficialSource",
    "call_with_fallback",
    "hkex_report_period_of_title",
    "pdf_name_of",
    "report_period_of_title",
]
