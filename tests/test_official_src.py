"""官方源兜底（巨潮 / 披露易）的离线测试（任务 M1-3）。

**全离线**：注入假 session（按 URL 返回预置响应），sleep 注入为记录器（不真等待），
缓存目录用 ``tmp_path``。
覆盖 M1-3 的验收要点「主源失败时自动调用」，以及检索 / 下载 / 缓存 / 节流 / 重试。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from src import config
from src.datasource import base
from src.datasource.official_src import (
    CNINFO_STATIC_BASE,
    HKEX_SEARCH_URL,
    FallbackResult,
    OfficialSource,
    call_with_fallback,
    pdf_name_of,
    report_period_of_title,
)

FIXED_DAY = date(2026, 10, 14)


# ── 测试替身 ──────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, *, payload: object = None, content: bytes = b"", status: int = 200) -> None:
        self._payload = payload
        self.content = content
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


def cninfo_handler(periods, *, session_box=None):
    """巨潮：公告检索返回 JSON；PDF 下载返回字节。"""

    def handler(method, url, kwargs):
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


def hkex_handler(documents):
    def handler(method, url, kwargs):
        if "titleSearchServlet" in url:
            # 披露易把结果塞在 JSON 字符串里（防御式解析要覆盖这个分支）
            return FakeResponse(payload={"result": json.dumps(documents)})
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
        ("2024年半年度报告", "2024-12-31"),  # 骨架只认年份，题型过滤由检索参数负责
    ],
)
def test_report_period_of_title(title: str, expected: str | None) -> None:
    assert report_period_of_title(title) == expected


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


def test_hk_code_routes_to_hkex_and_parses_json_string(tmp_path) -> None:
    documents = [{"TITLE": "2024年年報", "FILE_LINK": "/listedco/listconews/sehk/2025/0428/a.pdf"}]
    session = FakeSession(hkex_handler(documents))
    source = make_source(hkex_handler(documents), tmp_path, session=session)

    located = source.annual_report_documents("HK00700")
    assert [doc["report_period"] for doc in located] == ["2024-12-31"]
    assert (
        located[0]["pdf_url"] == "https://www1.hkexnews.hk/listedco/listconews/sehk/2025/0428/a.pdf"
    )
    assert session.calls[0]["url"] == HKEX_SEARCH_URL


def test_broken_hkex_payload_yields_no_documents(tmp_path) -> None:
    def handler(method, url, kwargs):
        return FakeResponse(payload={"result": "{not json"})

    assert make_source(handler, tmp_path).annual_report_documents("HK00700") == []


# ── 重试 / 退避 / 节流 ───────────────────────────────────────────────────
def test_retry_then_success_uses_backoff(tmp_path) -> None:
    delays: list[float] = []
    attempts = {"n": 0}

    def handler(method, url, kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("flaky")
        return FakeResponse(payload={"announcements": []})

    source = make_source(handler, tmp_path, sleep=delays.append)
    source.annual_report_documents("SH600900")

    assert attempts["n"] == 3
    assert delays == [1, 3]  # 退避：第 3 次成功，无需再等


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

    source.annual_report_documents("SH600900")  # 公告 + PDF = 2 次请求
    assert len(session.calls) == 2
    assert delays == [config.FETCH_MIN_INTERVAL_SECONDS]  # 首次不睡，第二次前补足间隔


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
    """官方源接下来也会因 M1-3 边界（解析未实现）失败，但**回退确实发生了**。"""
    official = make_source(cninfo_handler([2024]), tmp_path)
    with pytest.raises(base.DataSourceError, match="已定位 1 份年报"):
        call_with_fallback(_FailSource(), official, "financials", "SH600900")


def test_fallback_result_records_reason(tmp_path) -> None:
    """把官方源的解析缺口绕开（打桩）后，回退结果应带 degraded 与原因。"""
    official = make_source(cninfo_handler([2024]), tmp_path)
    official.parse_annual_report = lambda doc: {
        "code": doc["code"],
        "report_period": doc["report_period"],
    }

    result = call_with_fallback(_FailSource(), official, "financials", "SH600900")
    assert result.degraded is True
    assert result.source == "official"
    assert result.reason is not None and "主源挂了" in result.reason
    assert result.data[0]["report_period"] == "2024-12-31"


# ── M1-3 边界：解析未实现，但必须给出可行动的信息 ───────────────────────
def test_financials_reports_located_reports_then_fails(tmp_path) -> None:
    source = make_source(cninfo_handler([2022, 2023, 2024]), tmp_path)
    with pytest.raises(base.DataSourceError) as excinfo:
        source.financials("SH600900")

    message = str(excinfo.value)
    assert "三表解析尚未实现" in message
    assert "已定位 3 份年报" in message
    assert "2024-12-31" in message


def test_financials_without_announcements_says_so(tmp_path) -> None:
    source = make_source(cninfo_handler([]), tmp_path)
    with pytest.raises(base.DataSourceError, match="未检索到年报公告"):
        source.financials("SH600900")
