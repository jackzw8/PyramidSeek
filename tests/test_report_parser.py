"""年报 PDF → 三表解析的离线测试（闭环 D-11 的核心逻辑）。

用**手写的中文三表片段**（贴近真实版式）覆盖：定位、科目匹配、数字解析、单位换算、
本期+上期两列、必填缺失报错、扫描件报错。
"""

from __future__ import annotations

import pytest

from src import config
from src.datasource import base, report_parser

# ── 贴近真实版式的三表片段（单位：元）────────────────────────────────────
INCOME_TEXT = """合并利润表
项目 附注 本期金额 上期金额
一、营业总收入 1 845,000,000.00 782,000,000.00
其中：营业成本 2 321,000,000.00 300,000,000.00
二、营业利润 3 400,000,000.00 360,000,000.00
归属于母公司所有者的净利润 4 325,000,000.00 272,000,000.00
"""

BALANCE_TEXT = """合并资产负债表
项目 附注 期末余额 期初余额
资产总计 1 620,000,000,000.00 6,100,000,000,000.00
负债合计 2 352,000,000,000.00 3,480,000,000,000.00
归属于母公司所有者权益合计 3 268,000,000,000.00 2,620,000,000,000.00
货币资金 4 900,000,000.00 800,000,000.00
"""

CASHFLOW_TEXT = """合并现金流量表
项目 附注 本期金额 上期金额
经营活动产生的现金流量净额 1 596,000,000.00 647,000,000.00
投资活动产生的现金流量净额 2 -450,000,000.00 (400,000,000.00)
筹资活动产生的现金流量净额 3 — -100,000,000.00
"""

FULL_TEXT = f"{INCOME_TEXT}\n{BALANCE_TEXT}\n{CASHFLOW_TEXT}"


# ── 数字解析 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("845,000,000.00", 845_000_000.0),
        ("1,234", 1234.0),
        ("1234.5", 1234.5),
        ("(400,000,000.00)", -400_000_000.0),  # 括号 = 负数
        ("-450,000,000.00", -450_000_000.0),  # 负号
        ("0", 0.0),  # 0 是有效值（家法 R11）
        ("0.00", 0.0),
        ("—", None),
        ("-", None),
        ("", None),
        ("不是数字", None),
    ],
)
def test_parse_amount(token: str, expected: float | None) -> None:
    assert report_parser.parse_amount(token) == expected


def test_parse_amount_handles_fullwidth_comma() -> None:
    assert report_parser.parse_amount("1，234.50") == 1234.5


# ── 科目名归一 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("一、营业总收入", "营业总收入"),
        ("（一）营业收入", "营业收入"),
        ("其中：营业成本", "营业成本"),
        ("1.货币资金", "货币资金"),
        ("一年内到期的非流动负债", "一年内到期的非流动负债"),  # 中文数字前缀不应被误剥
        ("归属于母公司所有者的净利润", "归属于母公司所有者的净利润"),
    ],
)
def test_normalize_subject(raw: str, expected: str) -> None:
    assert report_parser.normalize_subject(raw) == expected


def test_parse_line_skips_header_and_table_of_contents() -> None:
    assert report_parser.parse_line("项目 附注 本期金额 上期金额") is None
    assert report_parser.parse_line("2024年12月31日") is None  # 数字开头 → 不是科目行
    assert report_parser.parse_line("") is None
    parsed = report_parser.parse_line("一、营业总收入 1 845,000,000.00 782,000,000.00")
    assert parsed == ("营业总收入", [845_000_000.0, 782_000_000.0])


# ── 单位识别 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("单位：元\n资产总计 1 100", 1.0),
        ("单位：千元\n资产总计 1 100", 1e3),
        ("单位：万元\n资产总计 1 100", 1e4),
        ("单位：百万元\n资产总计 1 100", 1e6),
        ("没有任何单位提示", 1.0),  # 默认按「元」
    ],
)
def test_detect_unit_multiplier(text: str, expected: float) -> None:
    assert report_parser.detect_unit_multiplier(text) == expected


# ── 定位 ──────────────────────────────────────────────────────────────────
def test_split_statements_finds_three_tables() -> None:
    statements = report_parser.split_statements(FULL_TEXT)
    assert set(statements) == {"income", "balance", "cashflow"}
    assert "营业总收入" in statements["income"]
    assert "资产总计" in statements["balance"]
    assert "经营活动产生的现金流量净额" in statements["cashflow"]


def test_split_statements_prefers_merged_when_duplicated() -> None:
    text = "母公司利润表\n营业总收入 1 100\n" "合并利润表\n营业总收入 1 999\n"
    statements = report_parser.split_statements(text)
    assert "999" in statements["income"]  # 合并口径优先


def test_split_statements_returns_empty_for_unrelated_text() -> None:
    assert report_parser.split_statements("这是一份没有任何报表的公告。") == {}


# ── 科目取值 ──────────────────────────────────────────────────────────────
def test_parse_statement_reads_both_columns() -> None:
    values = report_parser.parse_statement(INCOME_TEXT, kind="income")
    assert values["revenue"] == [845_000_000.0, 782_000_000.0]
    assert values["net_profit"] == [325_000_000.0, 272_000_000.0]
    assert values["cost"] == [321_000_000.0, 300_000_000.0]


def test_parse_statement_prefers_exact_subject_match() -> None:
    """``营业收入`` 与 ``营业总收入`` 不能互相误配。"""
    text = "营业收入 1 100\n营业总收入 1 999\n"
    values = report_parser.parse_statement(text, kind="income")
    assert values["revenue"][0] == 100.0


def test_missing_subjects_reports_required_gaps() -> None:
    values = report_parser.parse_statement("资产总计 1 620,000,000,000.00", kind="balance")
    assert report_parser.missing_subjects("balance", values) == ["equity"]


# ── 组装：本期 + 上期 ────────────────────────────────────────────────────
def test_build_report_rows_produces_current_and_previous_period() -> None:
    rows = report_parser.build_report_rows(
        "SH600900", FULL_TEXT, report_period="2024-12-31", fetched_at="2026-10-14T00:00:00Z"
    )

    assert [row["report_period"] for row in rows] == ["2024-12-31", "2023-12-31"]
    current, previous = rows

    # 元 → 亿元（2 位小数）
    assert current["revenue"] == 8.45
    assert current["net_profit"] == 3.25
    assert current["total_assets"] == 6200.0
    assert current["equity"] == 2680.0
    assert current["op_cashflow"] == 5.96
    assert current["gross_profit"] == round(
        8.45 - 3.21, config.AMOUNT_DECIMALS
    )  # revenue − cost 反算

    assert previous["revenue"] == 7.82
    assert previous["net_profit"] == 2.72
    assert previous["op_cashflow"] == 6.47
    assert previous["icf"] == -4.0  # 括号负数
    assert previous["fcf"] == -1.0

    assert current["report_type"] == "annual"
    assert current["is_cumulative"] is False
    assert current["source"] == "official"
    assert current["effective_date"] is None  # 由 M1-4 清洗阶段补齐（家法 R9）


def test_build_report_rows_satisfies_financial_contract() -> None:
    rows = report_parser.build_report_rows("SH600900", FULL_TEXT, report_period="2024-12-31")
    for row in rows:
        assert set(base.FINANCIAL_REQUIRED_FIELDS) <= set(row)


def test_build_report_rows_scales_by_unit() -> None:
    text = FULL_TEXT.replace(
        "项目 附注 本期金额 上期金额", "单位：万元\n项目 附注 本期金额 上期金额"
    )
    rows = report_parser.build_report_rows("SH600900", text, report_period="2024-12-31")
    # 845,000,000 万元 = 8.45 万亿元 = 84500 亿元
    assert rows[0]["revenue"] == 84500.0


def test_build_report_rows_rejects_missing_required_subject() -> None:
    text = "合并利润表\n营业总收入 1 100,000,000.00\n"  # 缺净利润
    with pytest.raises(base.DataSourceError) as excinfo:
        report_parser.build_report_rows("SH600900", text, report_period="2024-12-31")
    assert "必填科目缺失" in str(excinfo.value)
    assert "net_profit" in str(excinfo.value)


def test_build_report_rows_rejects_text_without_statements() -> None:
    with pytest.raises(base.DataSourceError, match="未在年报正文中定位到三表"):
        report_parser.build_report_rows(
            "SH600900", "本公告不包含财务报表。", report_period="2024-12-31"
        )


# ── 抽取层（注入 fake reader，不真读 PDF）────────────────────────────────
class FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class FakeReader:
    def __init__(self, *_args, **_kwargs) -> None:
        self.pages = [FakePage(INCOME_TEXT), FakePage(BALANCE_TEXT), FakePage(CASHFLOW_TEXT)]


def test_extract_text_joins_pages(tmp_path) -> None:
    text = report_parser.extract_text(tmp_path / "x.pdf", reader_factory=FakeReader)
    assert "营业总收入" in text
    assert "资产总计" in text
    assert "经营活动产生的现金流量净额" in text


def test_extract_text_returns_empty_for_scanned_pdf(tmp_path) -> None:
    class BlankReader:
        def __init__(self, *_args, **_kwargs) -> None:
            self.pages = [FakePage("")]

    assert report_parser.extract_text(tmp_path / "scan.pdf", reader_factory=BlankReader) == ""
