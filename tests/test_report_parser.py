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


# ── 真实年报版式（2026-10-14 长江电力 2025 年报验证时暴露的三个问题）──────
REAL_INCOME_TEXT = """合并利润表
项目 附注 2025 年度 2024 年度
一、营业总收入 86,241,940,222.20 84,491,870,566.52
其中：营业收入 七、61 86,241,940,222.20 84,491,870,566.52
二、营业总成本 48,766,372,374.15 50,268,818,794.54
其中：营业成本 七、61 33,057,840,034.77 34,528,346,186.67
五、净利润（净亏损以“－”号填列） 34,949,270,887.55 32,930,199,395.19
（一）按经营持续性分类
1.持续经营净利润（净亏损以
“－”号填列） 34,949,270,887.55 32,930,199,395.19
（二）按所有权归属分类
1.归属于母公司股东的净利润（净
亏损以“-”号填列） 34,502,809,176.39 32,496,172,808.65
2.少数股东损益（净亏损以“-”号
填列） 446,461,711.16 434,026,586.54
"""

REAL_BALANCE_TEXT = """合并资产负债表
项目 附注 期末余额 期初余额
资产总计 七、24 559,208,340,046.84 566,585,128,973.81
负债合计 七、35 325,847,678,729.19 344,500,336,848.80
所有者权益（或股东权益）：
归属于母公司股东权益合
计 221,338,010,639.92 210,348,910,895.97
少数股东权益 12,022,650,677.73 11,735,182,817.79
股东权益合计 233,360,661,317.65 222,084,093,713.76
负债和股东权益总计 559,208,340,046.84 566,585,128,973.81
"""


def test_fullwidth_minus_inside_chinese_quotes_is_part_of_subject() -> None:
    """`（净亏损以“－”号填列）` 里的全角减号不能当成空值 token（否则整行金额列错位）。"""
    parsed = report_parser.parse_line("五、净利润（净亏损以“－”号填列） 34,949,270,887.55")
    assert parsed is not None
    subject, amounts = parsed
    assert subject == "净利润（净亏损以“－”号填列）"
    assert amounts == [34_949_270_887.55]


def test_standalone_dash_is_still_blank() -> None:
    """独立成列的「—」仍是空值符号（如现金流量表里本期无金额）。"""
    parsed = report_parser.parse_line("经营活动产生的现金流量净额 1 — 647,000,000.00")
    assert parsed is not None
    assert parsed[1] == [None, 647_000_000.0]


def test_label_split_across_lines_is_recovered() -> None:
    """标签被换行切断（`归属于母公司股东权益合` / `计 221,…`）也要能识别。"""
    values = report_parser.parse_statement(REAL_BALANCE_TEXT, kind="balance")
    assert values["equity"][0] == 221_338_010_639.92
    assert values["total_assets"][0] == 559_208_340_046.84
    assert values["total_liabilities"][0] == 325_847_678_729.19


def test_real_income_layout_picks_attributable_net_profit() -> None:
    """真实版式下 net_profit 必须取**归母**（345.03 亿），不能取总净利润（349.49 亿）。"""
    values = report_parser.parse_statement(REAL_INCOME_TEXT, kind="income")
    assert values["revenue"][0] == 86_241_940_222.20
    assert values["cost"][0] == 33_057_840_034.77
    assert values["net_profit"][0] == 34_502_809_176.39
    assert values["net_profit"][1] == 32_496_172_808.65  # 上期列


def test_bare_net_profit_is_not_an_accepted_subject() -> None:
    """裸「净利润」是总口径：宁可返回 None 让上层报错，也不写错值。"""
    assert "净利润" not in report_parser.SUBJECTS["income"]["net_profit"]


def test_build_report_rows_works_on_real_layout() -> None:
    """把真实版式片段拼成一份"迷你年报"，验证三段式端到端仍能产出标准层行。"""
    cashflow = "合并现金流量表\n" "经营活动产生的现金流量净额 60,562,925,570.41 59,000,000,000.00\n"
    text = f"{REAL_INCOME_TEXT}\n{REAL_BALANCE_TEXT}\n{cashflow}"
    rows = report_parser.build_report_rows("SH600900", text, report_period="2025-12-31")

    assert [row["report_period"] for row in rows] == ["2025-12-31", "2024-12-31"]
    current = rows[0]
    assert current["revenue"] == round(86_241_940_222.20 / 1e8, 2)
    assert current["net_profit"] == round(34_502_809_176.39 / 1e8, 2)
    assert current["equity"] == round(221_338_010_639.92 / 1e8, 2)


# ── 港股版式（2026-10-14 用腾讯 2025 年报实测取证，见《计划完成情况》D-24）────
# 繁体 + HKFRS 命名：「綜合收益表」是利润表、「財務狀況表」是资产负债表、
# 单位写成「人民幣百萬元」（**没有**「单位：」前缀）、用「盈利」而不是「溢利」。
HK_SUMMARY_TEXT = """二零二五年年報
財務概要
簡明綜合全面收益表
截至十二月三十一日止年度
人民幣百萬元
收入 560,118 554,552 609,015 660,257 751,766
毛利 245,944 238,746 293,109 349,246 422,593
"""

HK_REPORT_TEXT = """綜合收益表
截至二零二五年十二月三十一日止年度
人民幣百萬元
附註 二零二五年 二零二四年
收入 751,766 660,257
銷售成本 (329,173) (311,011)
本公司權益持有人應佔盈利 224,842 194,073
綜合財務狀況表
人民幣百萬元
資產總額 2,038,986 1,780,995
負債總額 797,921 727,099
本公司權益持有人應佔權益 1,241,065 1,053,896
現金及現金等價物 141,000 132,000
綜合現金流量表
人民幣百萬元
經營活動所得現金淨額 210,000 200,000
"""


def test_hk_titles_are_located_by_traditional_names() -> None:
    statements = report_parser.split_statements(HK_REPORT_TEXT)
    assert set(statements) == {"income", "balance", "cashflow"}
    assert "收入" in statements["income"]
    assert "資產總額" in statements["balance"]
    assert "經營活動所得現金淨額" in statements["cashflow"]


def test_summary_table_is_not_used_as_data_source() -> None:
    """「簡明」概要表（五年财务概要）必须被跳过 —— 否则会把五年前的数当本期。

    概要表排在正式报表**之前**（真实年报就是第 4 页 vs 第 129 页），若不过滤就会先命中它。
    """
    rows = report_parser.build_report_rows(
        "HK00700", HK_SUMMARY_TEXT + HK_REPORT_TEXT, report_period="2025-12-31"
    )
    current = rows[0]
    # 概要表首列是 560,118（五年前）；正式表首列才是 751,766
    assert current["revenue"] == round(751_766 * 1e6 / 1e8, 2) == 7517.66


def test_toc_entries_do_not_win_over_real_statements() -> None:
    """**目录页陷阱**：目录里三个标题紧挨着，若按"先到先得"选出的是几字符的空片段。

    真实年报（腾讯 2025）目录就是 `130 綜合收益表 / 131 綜合財務狀況表 / 138 綜合現金流量表`，
    2026-10-14 实测时正是这里导致「必填科目全缺」。
    """
    toc = "目錄\n130 綜合收益表\n131 綜合財務狀況表\n138 綜合現金流量表\n"
    rows = report_parser.build_report_rows(
        "HK00700", toc + HK_REPORT_TEXT, report_period="2025-12-31"
    )

    assert rows[0]["revenue"] == 7517.66  # 取的是真报表，不是目录行
    assert rows[0]["total_assets"] == 20389.86


def test_hk_subjects_and_hundred_million_unit() -> None:
    """「人民幣百萬元」→ ×1e6；科目用繁体 + 「盈利」写法。"""
    rows = report_parser.build_report_rows("HK00700", HK_REPORT_TEXT, report_period="2025-12-31")
    current = rows[0]

    assert current["revenue"] == 7517.66  # 751,766 百万元
    # 成本写在括号里（港股惯例，被解析成负数）→ 毛利必须是 revenue − |cost|，
    # 不能是 revenue + cost（实测：腾讯毛利会变成 10809 亿，而年报里是 4225.93 亿）
    assert current["gross_profit"] == 4225.93
    assert current["net_profit"] == 2248.42
    assert current["total_assets"] == 20389.86
    assert current["equity"] == 12410.65
    assert current["op_cashflow"] == 2100.00
    assert current["currency"] == "CNY"
    # 上期列
    assert rows[1]["revenue"] == 6602.57


def test_unit_is_detected_per_statement_not_from_document_head() -> None:
    """单位必须按**单张表**识别。

    整本的第 1 页只有封面，从那儿找不到单位会默认成「元」→ 结果整体差 100 倍。
    """
    assert report_parser.detect_unit_multiplier(HK_REPORT_TEXT[:200]) == 1e6
    # 同样的「人民幣百萬元」若放在很后面（超出 head 窗口），识别不到 → 默认「元」
    assert (
        report_parser.detect_unit_multiplier("封面\n" * 500 + "人民幣百萬元", head_chars=200) == 1.0
    )


def test_detect_currency_only_accepts_currency_next_to_unit() -> None:
    assert report_parser.detect_currency("人民幣百萬元") == "CNY"
    assert report_parser.detect_currency("港幣千元") == "HKD"
    assert report_parser.detect_currency("US$ million") is None  # 未支持的写法 → 不猜
    # 正文里顺带提到别的币种不改判报表币种
    assert report_parser.detect_currency("本公司以港幣派息，報表以人民幣百萬元列示") == "CNY"


def test_short_candidates_are_exact_only() -> None:
    """「收入」这类短名只允许精确匹配：否则会命中「其他收入」这种**完全不同**的行。"""
    text = "綜合收益表\n人民幣百萬元\n其他收入 9,999 9,999\n"
    values = report_parser.parse_statement(text, kind="income")
    assert values["revenue"] == [None]  # 宁可失败，也不把「其他收入」当营业收入


def test_hk_missing_required_subject_still_errors() -> None:
    """缺必填科目依然报错（D-11 原则在港股版式下同样成立）。"""
    broken = HK_REPORT_TEXT.replace("本公司權益持有人應佔盈利 224,842 194,073\n", "")
    with pytest.raises(base.DataSourceError, match="必填科目缺失"):
        report_parser.build_report_rows("HK00700", broken, report_period="2025-12-31")


# ── 港股真实版式的三条硬骨头（2026-10-14 实测取证，D-24 闭环时补的用例）────
GROUPED_TOTAL_TEXT = """綜合收益表
人民幣百萬元
收入
－增值服務 369,281 319,168
－其他 8,077 7,759
6 751,766 660,257
下列人士應佔：
本公司權益持有人 224,842 194,073
"""


def test_group_heading_takes_the_unlabelled_total() -> None:
    """**「组头 + 明细 + 无标签合计」**版式：`收入` 的值在明细之后的合计行。

    腾讯年报利润表就是这个样子：`收入` 单独一行，下面是明细，合计行**没有标签**。
    只做相邻拼接时 `收入` 永远取不到值（曾因此选错整张表）。
    """
    rows = report_parser.build_report_rows(
        "HK00700", GROUPED_TOTAL_TEXT, report_period="2025-12-31"
    )
    # 751,766 百万元 → 7517.66 亿元；绝不能取成第一个明细 369,281
    assert rows[0]["revenue"] == 7517.66
    assert rows[1]["revenue"] == 6602.57


def test_values_only_detection() -> None:
    """「纯数值行」判定：合计行没有科目名，别把它当科目行、也别把它当标题。"""
    assert report_parser._is_values_only("6 751,766 660,257")
    assert report_parser._is_values_only("1,154,152 973,548")
    assert not report_parser._is_values_only("收入")
    assert not report_parser._is_values_only("增值服務 369,281 319,168")


RUNNING_HEADER_TEXT = """綜合財務狀況表
人民幣百萬元
資產總額 2,000,000 1,900,000
綜合財務狀況表
本公司權益持有人應佔權益
－股本 10 10
－保留盈利 1,100 1,000
1,150,000 973,548
負債總額 900,000 900,000
綜合現金流量表
人民幣百萬元
經營活動所得現金流量淨額 300,000 250,000
"""


def test_statement_span_runs_to_the_next_different_statement() -> None:
    """**页眉陷阱**：港股报表每页都有页眉（同名标题逐页重复）。

    若把"下一个同名标题"当切分终点，切出来的永远是页眉之间的缝隙（实测只有 743 字符），
    必填科目必然缺失。终点必须是"下一张**别的**表"。
    """
    rows = report_parser.build_report_rows(
        "HK00700", RUNNING_HEADER_TEXT, report_period="2025-12-31"
    )
    assert len(rows) == 2  # 本期 + 上期
    assert rows[0]["total_assets"] == 20000.0
    assert rows[0]["equity"] == 11500.0  # 无标签合计行 1,150,000
    assert rows[0]["total_liabilities"] == 9000.0
    assert rows[0]["op_cashflow"] == 3000.0


@pytest.mark.parametrize(
    "label",
    ["經營活動所得現金流量淨額", "經營活動所得現金淨額", "经营活动产生的现金流量净额"],
)
def test_hk_and_a_share_cashflow_wording_variants(label: str) -> None:
    """现金流科目在两地/不同发行人有多种写法（腾讯用带「流量」的那条）。"""
    text = f"綜合現金流量表\n人民幣百萬元\n{label} 300,000 250,000\n"
    values = report_parser.parse_statement(text, kind="cashflow")
    assert values["op_cashflow"] == [300_000.0, 250_000.0]


# ── 抽取器链路：pymupdf 优先、pypdf 兜底（D-24）────────────────────────────
def test_extract_text_prefers_first_non_empty_extractor(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    def fake_pymupdf(_path: str) -> str:
        calls.append("pymupdf")
        return ""

    def fake_pypdf(_path: str) -> str:
        calls.append("pypdf")
        return "正文"

    monkeypatch.setattr(
        report_parser, "_EXTRACTORS", {"pymupdf": fake_pymupdf, "pypdf": fake_pypdf}
    )
    monkeypatch.setattr(report_parser.config, "PDF_EXTRACTORS", ("pymupdf", "pypdf"))

    assert report_parser.extract_text(tmp_path / "a.pdf") == "正文"
    assert calls == ["pymupdf", "pypdf"]  # 前者抽空 → 继续试下一个


def test_extract_text_skips_missing_library(monkeypatch, tmp_path) -> None:
    def not_installed(_path: str) -> str:
        raise ImportError("pymupdf 未安装")

    monkeypatch.setattr(
        report_parser, "_EXTRACTORS", {"pymupdf": not_installed, "pypdf": lambda _p: "OK"}
    )
    monkeypatch.setattr(report_parser.config, "PDF_EXTRACTORS", ("pymupdf", "pypdf"))

    assert report_parser.extract_text(tmp_path / "a.pdf") == "OK"


def test_extract_text_raises_when_every_extractor_fails(monkeypatch, tmp_path) -> None:
    def boom(_path: str) -> str:
        raise ValueError("坏 PDF")

    monkeypatch.setattr(report_parser, "_EXTRACTORS", {"pymupdf": boom})
    monkeypatch.setattr(report_parser.config, "PDF_EXTRACTORS", ("pymupdf",))

    with pytest.raises(base.DataSourceError, match="PDF 抽取失败"):
        report_parser.extract_text(tmp_path / "a.pdf")
