"""官方源兜底（巨潮 / 披露易）的离线测试（任务 M1-3）。

**全离线**：注入假 session（按 URL 返回预置响应），sleep 注入为记录器（不真等待），
缓存目录用 ``tmp_path``。
覆盖 M1-3 的验收要点「主源失败时自动调用」，以及检索 / 下载 / 缓存 / 节流 / 重试。
"""

from __future__ import annotations

from datetime import date

import pytest

from src import config
from src.datasource import base
from src.datasource.official_src import (
    CNINFO_STATIC_BASE,
    HKEX_PREFIX_URL,
    HKEX_SEARCH_URL,
    FallbackResult,
    OfficialSource,
    call_with_fallback,
    hkex_report_period_of_title,
    pdf_name_of,
    report_period_of_title,
)

FIXED_DAY = date(2026, 10, 14)


# ── 测试替身 ──────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(
        self,
        *,
        payload: object = None,
        content: bytes = b"",
        status: int = 200,
        text: str = "",
    ) -> None:
        self._payload = payload
        self.content = content
        self.text = text
        self.status_code = status

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("响应不含 JSON")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """记录每次请求，并按 URL 交给 handler 决定返回什么。"""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return self._handler(method, url, kwargs)


CNINFO_PDF = b"%PDF-1.4 fake annual report"

# 贴近真实版式的三表片段（单位：元），供 D-11 解析链路复用
MINI_REPORT_TEXT = """合并利润表
项目 附注 本期金额 上期金额
营业总收入 1 845,000,000.00 782,000,000.00
营业成本 2 321,000,000.00 300,000,000.00
归属于母公司所有者的净利润 3 325,000,000.00 272,000,000.00
合并资产负债表
项目 附注 期末余额 期初余额
资产总计 1 620,000,000,000.00 610,000,000,000.00
负债合计 2 352,000,000,000.00 348,000,000,000.00
归属于母公司所有者权益合计 3 268,000,000,000.00 262,000,000,000.00
合并现金流量表
项目 附注 本期金额 上期金额
经营活动产生的现金流量净额 1 596,000,000.00 647,000,000.00
"""


def cninfo_handler(periods, *, org_id: str = "gssh0600900", session_box=None):
    """巨潮：orgId 查询 + 公告检索返回 JSON；PDF 下载返回字节。"""

    def handler(method, url, kwargs):
        if "topSearch" in url:
            items = [{"code": "600900", "orgId": org_id, "zwjc": "长江电力"}] if org_id else []
            return FakeResponse(payload=items)
        if "hisAnnouncement" in url:
            return FakeResponse(
                payload={
                    "announcements": [
                        {
                            "announcementTitle": f"长江电力{year}年年度报告",
                            "adjunctUrl": f"finalpage/2025-04-30/{year}.PDF",
                            "announcementTime": 1746000000000,
                            "secCode": "600900",
                        }
                        for year in periods
                    ]
                }
            )
        return FakeResponse(content=CNINFO_PDF)

    return handler


def hkex_handler(years=(2024,), *, lang="zh", stock_id=12345):
    """披露易：① stockId 查询返回 JSONP 文本；② 检索返回 **HTML 表格**；③ PDF 返回字节。

    ``lang="both"`` 时同一年度同时给英文版与中文版（英文版排在前面，用于验证"优先中文"）。
    """

    def pdf_blocks() -> str:
        if lang == "both":
            suffixes = (".pdf", "_c.pdf")  # 英文在前，刻意考验"不是简单取第一条"
        else:
            suffixes = ("_c.pdf",) if lang == "zh" else (".pdf",)
        return "".join(
            f'<tr><td class="date">2025-04-28</td>'
            f'<td><a href="/listedco/listconews/sehk/2025/0428/{year}{suffix}">PDF</a></td>'
            f"<td>文件:{year} 年報</td></tr>"
            for year in years
            for suffix in suffixes
        )

    def handler(method, url, kwargs):
        if "prefix.do" in url:
            if not stock_id:
                return FakeResponse(text='callback({"stockInfo":[]})')
            return FakeResponse(text=f'callback({{"stockInfo":[{{"stockId":{stock_id}}}]}})')
        if "titlesearch" in url:
            return FakeResponse(text=f"<html><table>{pdf_blocks()}</table></html>")
        return FakeResponse(content=b"%PDF-1.4 hk annual report")

    return handler


def make_source(handler, tmp_path, **kwargs) -> OfficialSource:
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("today", lambda: FIXED_DAY)
    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("session", FakeSession(handler))
    return OfficialSource(cache_dir=tmp_path, **kwargs)


# ── 纯函数 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("长江电力2024年年度报告", "2024-12-31"),
        ("2023年年度报告（修订版）", "2023-12-31"),
        ("某某公司年度报告", None),
        ("", None),
        # 真实数据验证发现：`年度报告` 是 `半年度报告` 的子串，不能把半年报当成 12-31
        ("长江电力2025年半年度报告", None),
        ("某某公司2025年第一季度报告", None),
        ("某某公司2024年年度报告摘要", None),
        ("某某公司2024年年報", "2024-12-31"),  # 港股用词（年報）仍能识别
    ],
)
def test_report_period_of_title(title: str, expected: str | None) -> None:
    assert report_period_of_title(title) == expected


def test_non_annual_markers_are_excluded() -> None:
    """半年报 / 季报 / 摘要一律不作为年报期次，避免凭空多出一个财年。"""
    from src.datasource.official_src import NOT_ANNUAL_MARKERS

    for marker in NOT_ANNUAL_MARKERS:
        assert report_period_of_title(f"某公司2024年{marker}报告") is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("年報 2023", "2023-12-31"),
        ("2024 年報", "2024-12-31"),
        ("[年報] 2022", "2022-12-31"),
        ("ANNUAL REPORT 2023", "2023-12-31"),
        ("2023 Annual Report", "2023-12-31"),
        # 披露易同一批返回里混着中期报告与 ESG 报告 —— 都不能当成年报
        ("中期報告 2023", None),
        ("Environmental, Social and Governance Report 2023", None),
        ("", None),
    ],
)
def test_hkex_report_period_of_title_only_accepts_annual(title: str, expected: str | None) -> None:
    """港股标题用「年報」而非「年度报告」，故与 A 股的解析器分开实现（D-21）。"""
    assert hkex_report_period_of_title(title) == expected


def test_pdf_name_is_ascii() -> None:
    assert pdf_name_of("SH600900", "2024-12-31") == "SH600900_2024-12-31.pdf"


# ── 职责边界：官方源不提供的东西必须说清原因 ─────────────────────────────
def test_official_source_refuses_non_financial_data(tmp_path) -> None:
    source = make_source(cninfo_handler([]), tmp_path)
    with pytest.raises(base.DataSourceError, match="不提供股票列表"):
        source.list_stocks("A")
    with pytest.raises(base.DataSourceError, match="不提供行业分类树"):
        source.industry_tree("A")
    with pytest.raises(base.DataSourceError, match="不提供日行情"):
        source.price_daily("SH600900")


def test_health_check_returns_bool(tmp_path) -> None:
    ok = make_source(lambda *_: FakeResponse(), tmp_path)
    assert ok.health_check() is True

    def boom(method, url, kwargs):
        raise ConnectionError("down")

    assert make_source(boom, tmp_path).health_check() is False


# ── 公告检索 → 年报定位 → PDF 下载缓存 ──────────────────────────────────
def test_annual_report_documents_locate_and_download(tmp_path) -> None:
    source = make_source(cninfo_handler([2021, 2022, 2023, 2024]), tmp_path)
    documents = source.annual_report_documents("SH600900", years=3)

    assert [doc["report_period"] for doc in documents] == ["2024-12-31", "2023-12-31", "2022-12-31"]
    first = documents[0]
    assert first["pdf_url"] == f"{CNINFO_STATIC_BASE}finalpage/2025-04-30/2024.PDF"
    assert first["title"] == "长江电力2024年年度报告"
    # PDF 已落盘且内容正确
    from pathlib import Path

    path = Path(first["local_path"])
    assert path.exists()
    assert path.read_bytes() == CNINFO_PDF
    assert path.parent.name == "2026-10-14"


def test_pdf_and_announcement_are_cached(tmp_path) -> None:
    session = FakeSession(cninfo_handler([2024]))
    source = make_source(cninfo_handler([2024]), tmp_path, session=session)
    source.annual_report_documents("SH600900")
    calls_after_first = len(session.calls)

    make_source(cninfo_handler([2024]), tmp_path, session=session).annual_report_documents(
        "SH600900"
    )
    assert len(session.calls) == calls_after_first  # 公告与 PDF 均命中缓存，零新请求


def test_cninfo_query_sends_code_and_orgid(tmp_path) -> None:
    """2026-10-14 真实验证：`stock` 必须是 `代码,orgId`，否则恒返回 0 条（D-16 根因）。"""
    from src.datasource.official_src import CNINFO_ANNUAL_CATEGORY

    session = FakeSession(cninfo_handler([2024]))
    source = make_source(cninfo_handler([2024]), tmp_path, session=session)
    source.annual_report_documents("SH600900")

    org_call = next(call for call in session.calls if "topSearch" in call["url"])
    assert org_call["kwargs"]["data"]["keyWord"] == "600900"

    query = next(call for call in session.calls if "hisAnnouncement" in call["url"])
    payload = query["kwargs"]["data"]
    assert payload["stock"] == "600900,gssh0600900"
    assert payload["column"] == "sse"
    assert payload["category"] == CNINFO_ANNUAL_CATEGORY
    assert payload["tabName"] == "fulltext"


def test_cninfo_without_orgid_raises_identifiable_error(tmp_path) -> None:
    """查不到 orgId 必须明确报错，而不是静默返回空集被当成"没有年报"。"""
    source = make_source(cninfo_handler([2024], org_id=""), tmp_path)
    with pytest.raises(base.DataSourceError, match="未查到巨潮 orgId"):
        source.annual_report_documents("SH600900")


def test_hk_code_resolves_stockid_then_searches_html(tmp_path) -> None:
    """**D-21 修正**：港股先 ``prefix.do`` 反查 stockId，再用 ``titlesearch.xhtml`` 检索。

    M1-3 的写法三处都错：直接拿 ``00700`` 当 stockId、端点用 ``titleSearchServlet.do``、
    按 JSON 解析（实际返回 HTML）—— 所以整条港股链路当时必然失败。
    """
    session = FakeSession(hkex_handler(years=(2024,)))
    source = make_source(hkex_handler(years=(2024,)), tmp_path, session=session)

    located = source.annual_report_documents("HK00700")

    assert [doc["report_period"] for doc in located] == ["2024-12-31"]
    assert located[0]["pdf_url"].endswith("/2024_c.pdf")

    # ① 反查 stockId：传股票代号与市场，不是把代号当 ID
    prefix_call = session.calls[0]
    assert prefix_call["url"] == HKEX_PREFIX_URL
    assert prefix_call["kwargs"]["params"]["name"] == "00700"
    assert prefix_call["kwargs"]["params"]["market"] == "SEHK"

    # ② 检索：带**内部 stockId** 与年报分类码，语言走 query
    assert session.calls[1]["url"] == HKEX_SEARCH_URL
    search = session.calls[1]["kwargs"]
    assert search["params"] == {"lang": "zh"}
    assert search["data"]["stockId"] == "12345"
    assert search["data"]["t1code"] == "40000"
    assert search["data"]["t2code"] == "40100"
    assert search["data"]["market"] == "SEHK"
    assert search["data"]["from"].endswith("0101") and search["data"]["to"].endswith("1231")


def test_hk_prefers_chinese_pdf_for_same_year(tmp_path) -> None:
    """中英文是两套文件：同一年度**优先中文版**（解析器认中文科目名），英文版只是兜底。"""
    source = make_source(hkex_handler(years=(2024,), lang="both"), tmp_path)

    located = source.annual_report_documents("HK00700")

    assert len(located) == 1  # 同一年度只留一份
    assert located[0]["pdf_url"].endswith("/2024_c.pdf")


def test_hk_without_stockid_raises_identifiable_error(tmp_path) -> None:
    """查不到 stockId 必须明确报错，而不是静默返回空集被当成"没有年报"。"""
    source = make_source(hkex_handler(stock_id=0), tmp_path)
    with pytest.raises(base.DataSourceError, match="未查到披露易 stockId"):
        source.annual_report_documents("HK00700")


def test_hk_empty_result_page_yields_no_documents(tmp_path) -> None:
    """检索页里没有 PDF 链接（该期次确实没披露年报）→ 空列表，不报错。"""

    def handler(method, url, kwargs):
        if "prefix.do" in url:
            return FakeResponse(text='callback({"stockInfo":[{"stockId":12345}]})')
        return FakeResponse(text="<html><table></table></html>")

    assert make_source(handler, tmp_path).annual_report_documents("HK00700") == []


# ── 重试 / 退避 / 节流 ───────────────────────────────────────────────────
def test_retry_then_success_uses_backoff(tmp_path) -> None:
    delays: list[float] = []
    attempts = {"n": 0}

    def handler(method, url, kwargs):
        if "topSearch" in url:  # 只让这一跳不稳定，便于精确计数
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("flaky")
            return FakeResponse(payload=[{"code": "600900", "orgId": "gssh0600900"}])
        return FakeResponse(payload={"announcements": []})

    source = make_source(handler, tmp_path, sleep=delays.append)
    source.annual_report_documents("SH600900")

    assert attempts["n"] == 3
    assert delays == [1, 3]  # 退避：第 3 次成功，无需再等（min_interval=0 故无节流等待）


def test_retry_exhausted_raises_identifiable_error(tmp_path) -> None:
    def handler(method, url, kwargs):
        raise ConnectionError("always down")

    source = make_source(handler, tmp_path)
    with pytest.raises(base.DataSourceError) as excinfo:
        source.annual_report_documents("SH600900")
    assert excinfo.value.source == "official"
    assert excinfo.value.code == "SH600900"


def test_requests_are_throttled_by_min_interval(tmp_path) -> None:
    delays: list[float] = []
    session = FakeSession(cninfo_handler([2024]))
    source = make_source(
        cninfo_handler([2024]),
        tmp_path,
        session=session,
        sleep=delays.append,
        min_interval=config.FETCH_MIN_INTERVAL_SECONDS,
    )

    source.annual_report_documents("SH600900")  # orgId + 公告 + PDF = 3 次请求
    assert len(session.calls) == 3
    # 首次不睡；其后每次请求前补足间隔（家法：遵守目标站点抓取规范）
    assert delays == [config.FETCH_MIN_INTERVAL_SECONDS] * 2


# ── 主源 → 官方源自动回退（M1-3 验收）────────────────────────────────────
class _OkSource:
    name = "akshare"

    def financials(self, code: str):
        return [{"code": code, "revenue": 1.0}]


class _FailSource:
    name = "akshare"

    def financials(self, code: str):
        raise base.DataSourceError("主源挂了", source="akshare", code=code)


def test_fallback_not_used_when_primary_succeeds(tmp_path) -> None:
    official = make_source(cninfo_handler([]), tmp_path)
    result = call_with_fallback(_OkSource(), official, "financials", "SH600900")

    assert isinstance(result, FallbackResult)
    assert result.degraded is False
    assert result.source == "akshare"
    assert result.reason is None


def test_fallback_used_when_primary_fails(tmp_path) -> None:
    """官方源这里也会失败（正文里没有三表），但**回退确实发生了**。"""
    official = make_source(
        cninfo_handler([2024]), tmp_path, text_extractor=lambda _path: "本公告不包含财务报表。"
    )
    with pytest.raises(base.DataSourceError, match="已定位 1 份年报"):
        call_with_fallback(_FailSource(), official, "financials", "SH600900")


def test_fallback_result_records_reason(tmp_path) -> None:
    """官方源解析成功时，回退结果应带 degraded 与原因。"""
    official = make_source(
        cninfo_handler([2024]), tmp_path, text_extractor=lambda _path: MINI_REPORT_TEXT
    )
    result = call_with_fallback(_FailSource(), official, "financials", "SH600900")

    assert result.degraded is True
    assert result.source == "official"
    assert result.reason is not None and "主源挂了" in result.reason
    assert {row["report_period"] for row in result.data} == {"2024-12-31", "2023-12-31"}


# ── D-11：年报 PDF → 三表结构化（官方源端到端）──────────────────────────
def test_financials_end_to_end_parses_located_reports(tmp_path) -> None:
    """检索 → 下载 → 解析：一份年报产出「本期 + 上期」两行（单位换算到亿元）。"""
    source = make_source(
        cninfo_handler([2024]), tmp_path, text_extractor=lambda _path: MINI_REPORT_TEXT
    )
    rows = source.financials("SH600900")

    assert [row["report_period"] for row in rows] == ["2024-12-31", "2023-12-31"]
    assert rows[0]["revenue"] == 8.45
    assert rows[1]["net_profit"] == 2.72
    assert rows[0]["source"] == "official"
    assert set(base.FINANCIAL_REQUIRED_FIELDS) <= set(rows[0])


def test_financials_reports_no_statements_with_located_list(tmp_path) -> None:
    """正文里定位不到三表时，错误信息要带上「已定位到哪几份年报」。"""
    source = make_source(
        cninfo_handler([2022, 2023, 2024]), tmp_path, text_extractor=lambda _path: "没有报表"
    )
    with pytest.raises(base.DataSourceError) as excinfo:
        source.financials("SH600900")

    message = str(excinfo.value)
    assert "未在年报正文中定位到三表" in message
    assert "已定位 3 份年报" in message
    assert "2024-12-31" in message


def test_financials_reports_scanned_pdf(tmp_path) -> None:
    """抽取为空（扫描件）必须当成失败，不能当成「没有数字」。"""
    source = make_source(cninfo_handler([2024]), tmp_path, text_extractor=lambda _path: "   ")
    with pytest.raises(base.DataSourceError, match="抽不到文本"):
        source.financials("SH600900")


def test_financials_without_announcements_says_so(tmp_path) -> None:
    source = make_source(cninfo_handler([]), tmp_path)
    with pytest.raises(base.DataSourceError, match="未检索到年报公告"):
        source.financials("SH600900")
