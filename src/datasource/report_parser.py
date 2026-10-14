"""年报 PDF → 标准层三表（闭环《计划完成情况》D-11）。

**分层归属**：本模块属于**数据源层**的内部工具（由 :mod:`official_src` 调用）——
它做的是「官方披露原文 → 结构化字段」，不是评分或产物逻辑；放在数据源层可避免
``datasource`` 反向依赖 ``pipeline``。

三段式设计，每段都能离线测：

1. **抽取** :func:`extract_text` —— PDF → 纯文本（``pypdf`` 延迟导入，reader 可注入）；
2. **定位** :func:`split_statements` —— 按标题切成 利润表 / 资产负债表 / 现金流量表
   （同名多张时**优先合并口径**）；
3. **解析** :func:`parse_statement` / :func:`build_report_rows` —— 科目行 → 各列金额
   （千分位、全角逗号、括号负数、``—`` / ``-`` 表空、单位换算），组装成标准层行。

**原则（延续 D-11）**：找不到必填科目就**报错并列出缺哪些**，绝不用 0 或猜测值填充。

**已知局限**（不改口径，先记录）：版式差异大的年报（跨页三表、扫描件、图片表格）需要
更细的抽取手段（如 ``pdfplumber`` 的表格识别或 OCR）。当前实现走"纯文本 + 科目匹配"，
**首次真跑全量时需抽查核对**（见《计划完成情况》D-11 / D-15）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import config
from .base import DataSourceError

# ── 表标题（切分锚点）─────────────────────────────────────────────────────
# 两套命名：A 股（简体 / 内地准则）+ 港股（繁体 / HKFRS）。
# 港股命名是 2026-10-14 用腾讯 2025 年报**实测取证**的（《计划完成情况》D-24）：
# 利润表叫「綜合收益表」、资产负债表叫「財務狀況表」，**不是**「损益表 / 资产负债表」。
STATEMENT_TITLES: dict[str, tuple[str, ...]] = {
    "income": (
        "合并利润表",
        "利润表",
        "綜合收益表",
        "綜合全面收益表",
        "综合收益表",
        "综合全面收益表",
    ),
    "balance": ("合并资产负债表", "资产负债表", "綜合財務狀況表", "財務狀況表", "财务状况表"),
    "cashflow": ("合并现金流量表", "现金流量表", "綜合現金流量表", "現金流量表"),
}
# 「合并口径」的三种写法（同名多张时优先取它，见 split_statements）
MERGED_MARKERS = ("合并", "合併", "綜合")
# 「摘要」类标题必须排除：A 股叫「…摘要」，港股叫「簡明…」（五年财务概要）
# —— 把概要表当成合并报表是最容易犯且最难发现的错（D-24 实测：腾讯年报第 4 页就有）
SUMMARY_MARKERS = ("摘要", "簡明", "概要")

# ── 单位（顺序敏感：先长后短，避免「元」吃掉「万元」）─────────────────────
UNIT_FACTORS: tuple[tuple[str, float], ...] = (
    ("百万元", 1e6),
    ("百萬元", 1e6),
    ("百萬", 1e6),
    ("千元", 1e3),
    ("万元", 1e4),
    ("萬元", 1e4),
    ("元", 1.0),
)
# A 股写法：「单位：元」
UNIT_PATTERN = re.compile(r"单位[：:]\s*(百万元|百萬元|千元|万元|萬元|元)")
# ── 币种（ISO 4217 三字母；港股可能是人民币 / 港币 / 美元报表）──────────────
CURRENCY_ALIASES: dict[str, str] = {
    "人民幣": "CNY",
    "人民币": "CNY",
    "RMB": "CNY",
    "CNY": "CNY",
    "港幣": "HKD",
    "港币": "HKD",
    "港元": "HKD",
    "HKD": "HKD",
    "HK$": "HKD",
    "美元": "USD",
    "美金": "USD",
    "USD": "USD",
    "US$": "USD",
}
# 港股写法：币种紧跟单位，如「人民幣百萬元」——**没有**「单位：」前缀（D-24 实测取证）
CURRENCY_UNIT_PATTERN = re.compile(
    r"(" + "|".join(re.escape(alias) for alias in CURRENCY_ALIASES) + r")"
    r"\s*(百万元|百萬元|百萬|千元|万元|萬元|元)"
)

# ── 三表科目（标准层字段 → 年报科目名候选，按优先级）──────────────────────
# 两套命名并存：A 股（简体）与港股（繁体 / HKFRS）。港股部分为 2026-10-14 用腾讯 2025
# 年报实测取证（D-24），注意它用「盈利」而不是「溢利」、「資產總額」而不是「资产总计」。
SUBJECTS: dict[str, dict[str, tuple[str, ...]]] = {
    "income": {
        "revenue": ("营业收入", "营业总收入", "收入", "營業額", "营业额", "總收入", "总收入"),
        "cost": ("营业成本", "营业总成本", "營業成本", "銷售成本", "销售成本"),
        # 只用**归母**口径：裸「净利润 / 盈利 / 溢利」都是含少数股东的总口径，两者不是一个数
        # （真实年报验证：长江电力 2025 总净利润 349.49 亿 vs 归母 345.03 亿）
        "net_profit": (
            "归属于母公司所有者的净利润",
            "归属于母公司股东的净利润",
            "本公司權益持有人應佔盈利",
            "本公司擁有人應佔盈利",
            "本公司股東應佔盈利",
            "歸屬於本公司股東的淨利潤",
            "歸屬於母公司股東的淨利潤",
        ),
    },
    "balance": {
        "total_assets": ("资产总计", "资产合计", "資產總額", "資產總計", "總資產"),
        "total_liabilities": ("负债合计", "負債總額", "負債合計", "總負債"),
        "equity": (
            "归属于母公司所有者权益合计",
            "归属于母公司股东权益合计",
            "所有者权益合计",
            # 归母口径：`權益總額` 是含少数股东的**总**权益，故不列入
            "本公司權益持有人應佔權益",
            "本公司擁有人應佔權益",
            "歸屬於母公司股東的權益",
        ),
        "cash": ("货币资金", "貨幣資金", "現金及現金等價物", "现金及现金等价物"),
    },
    "cashflow": {
        "op_cashflow": (
            "经营活动产生的现金流量净额",
            "經營活動所得現金淨額",
            "經營活動產生的現金流量淨額",
        ),
        "icf": ("投资活动产生的现金流量净额", "投資活動所得現金淨額"),
        "fcf": ("筹资活动产生的现金流量净额", "融資活動所得現金淨額"),
    },
}

# 这些候选名**只允许精确匹配**：太短或太常见，宽松（子串）匹配会命中
# 「其他收入」「經營盈利」这类**完全不同**的行 —— 宁可失败也不写错值（延续 D-11）。
EXACT_ONLY_CANDIDATES = ("收入", "营业额", "營業額", "總收入", "总收入", "總資產", "總負債")

# 每张表必须命中的科目（缺失 → 报错，不猜）
REQUIRED_SUBJECTS: dict[str, tuple[str, ...]] = {
    "income": ("revenue", "net_profit"),
    "balance": ("total_assets", "equity"),
    "cashflow": ("op_cashflow",),
}

# 空值符号（报表里"没有金额"的几种写法）
BLANK_TOKENS = ("", "-", "--", "—", "－", "–", "/", "不适用")
# 数字 / 空值符号。空值符号要求**前后是空白或行首尾**——否则会把中文括号里的全角减号
# （如「（净亏损以“－”号填列）」里的 －）当成空值 token，导致整行金额列错位。
# 这是 2026-10-14 用真实年报（长江电力 2025）验证时暴露的问题。
_NUMBER_PATTERN = re.compile(r"\(?\s*-?\d[\d,，]*(?:\.\d+)?\s*\)?|(?<!\S)[-—－–]{1,2}(?!\S)")
_SUBJECT_MARKER = re.compile(r"^(其中|加|减|其他)[：:]")
_SUBJECT_ORDER = re.compile(r"^[（(]?[一二三四五六七八九十\d]+[）)、.]\s*")


def _join_pages(reader: Any) -> str:
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _pypdf_text(pdf_path: str | Path) -> str:
    """用 ``pypdf`` 抽文本（A 股年报一直正常）。"""
    from pypdf import PdfReader  # noqa: PLC0415  （刻意延迟导入）

    return _join_pages(PdfReader(str(pdf_path)))


def _pymupdf_text(pdf_path: str | Path) -> str:
    """用 ``pymupdf`` 抽文本。

    它能正确解码**没有 ``/ToUnicode`` 映射的 CID 中文字体**（自带 CMap 兜底），
    而 pypdf 对这种字体只能输出错码位 —— 港股年报的普遍情况（D-24 实测：
    同一份腾讯年报，pypdf 抽出 0 个 CJK 字符、pymupdf 抽出 13 万个）。

    刻意延迟导入：未安装时由 :func:`extract_text` 自动回落到 pypdf。
    """
    import pymupdf  # noqa: PLC0415

    class _Page:
        def __init__(self, page: Any) -> None:
            self._page = page

        def extract_text(self, **_: object) -> str:
            return self._page.get_text("text")

    class _Reader:
        def __init__(self, path: str) -> None:
            self._doc = pymupdf.open(str(path))

        @property
        def pages(self) -> list[Any]:
            return [_Page(page) for page in self._doc]

    return _join_pages(_Reader(str(pdf_path)))


_EXTRACTORS: dict[str, Any] = {"pymupdf": _pymupdf_text, "pypdf": _pypdf_text}


def extract_text(pdf_path: str | Path, *, reader_factory: Any | None = None) -> str:
    """抽取 PDF 全文 → 纯文本。

    - 注入了 ``reader_factory`` 时只用它（离线用例走这条）；
    - 否则按 :data:`config.PDF_EXTRACTORS` 顺序尝试，**返回第一个非空结果**；
      未安装的抽取器自动跳过（``ImportError``），全部抽取器都报错时抛错而不是静默返回空串。

    返回空串表示"抽不到文本"（扫描件 PDF 需要 OCR，不在本期范围）——
    调用方必须把空串当成**失败**，不能当成"没有数字"。
    """
    if reader_factory is not None:
        return _join_pages(reader_factory(str(pdf_path)))

    errors: list[str] = []
    attempted = 0
    for name in config.PDF_EXTRACTORS:
        try:
            text = _EXTRACTORS[name](pdf_path)
        except ImportError:  # 没装这个库 → 试下一个
            continue
        except Exception as exc:  # noqa: BLE001  该抽取器解析失败 → 试下一个
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        attempted += 1
        if text.strip():
            return text
    if attempted == 0 and errors:
        raise DataSourceError(
            f"PDF 抽取失败（{Path(pdf_path).name}）：" + "；".join(errors), source="official"
        )
    return ""


def detect_unit_multiplier(text: str, *, head_chars: int = 2000) -> float:
    """识别报表单位（``单位：元`` 或 ``人民幣百萬元``）→ 换算到「元」的乘数。

    找不到时按「元」处理（年报三表正文的默认单位）。

    **调用方式很关键**：必须传**单张表的正文片段**（``split_statements`` 的输出），
    不能传整本年报 —— 港股的单位声明（``人民幣百萬元``）在报表页头部，从整本第 1 页
    往后找 2000 字符只会读到封面，于是默认成「元」，**结果整体差 100 倍**（D-24 实测教训）。
    """
    head = text[:head_chars]
    match = UNIT_PATTERN.search(head)
    if match is None:
        match = CURRENCY_UNIT_PATTERN.search(head)
    if match is None:
        return 1.0
    # 「单位：X」的单位在第 1 组；「币种+单位」的币种在第 1 组、单位在第 2 组
    unit = match.group(2) if match.re is CURRENCY_UNIT_PATTERN else match.group(1)
    return dict(UNIT_FACTORS)[unit]


def detect_currency(text: str, *, head_chars: int = 2000) -> str | None:
    """识别报表**币种** → ISO 4217 三字母（``CNY`` / ``HKD`` / ``USD``）；识别不出返回 ``None``。

    只认「**币种紧跟单位**」这一种最可靠的形式（``人民幣百萬元``）：正文里顺带提到别的
    币种（如「以港幣派息」）不该改判报表币种 —— **不确定时返回 ``None``，不猜**（家法 R11）。
    """
    match = CURRENCY_UNIT_PATTERN.search(text[:head_chars])
    return CURRENCY_ALIASES.get(match.group(1)) if match else None


def parse_amount(token: str | None) -> float | None:
    """报表数字串 → ``float``：支持千分位、全角逗号、括号负数；空值符号 → ``None``。

    ``0`` 是有效值，必须原样返回 ``0.0``（家法 R11）。
    """
    if token is None:
        return None
    text = str(token).strip().replace("，", ",").replace(" ", "")
    if text in BLANK_TOKENS or (text and set(text) <= {"-", "—", "－", "–"}):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "")
    if text.startswith("-"):
        negative = True
        text = text[1:]
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def normalize_subject(name: str) -> str:
    """归一科目名：去掉「其中：」「加：」「一、」「（1）」等前后缀与**内部空白**。

    去掉内部空白是必需的：真实年报里标签会被换行切断，:func:`_candidate_lines`
    拼接时会插入空格（``归属于母公司股东权益合 计``），不去掉就永远匹配不上。
    """
    text = _SUBJECT_MARKER.sub("", str(name).strip())
    text = _SUBJECT_ORDER.sub("", text)
    return text.replace(" ", "").replace("\u3000", "").strip()


def strip_ordinal(line: str) -> str:
    """去掉行首编号（``1.`` / ``（一）`` / ``七、``）。

    必须先去掉编号再找数字：否则 ``1.归属于母公司股东的净利润…`` 的首个"数字"是行号
    ``1``，subject 变成空串、整行被丢弃 —— 2026-10-14 真实年报验证暴露。
    """
    return _SUBJECT_ORDER.sub("", line.lstrip())


def _is_note_column(token: str) -> bool:
    """判断首个数字是否为「附注」列（年报三表首列的附注号：1、2、3… 这类裸小整数）。"""
    text = token.strip().strip("()")
    return text.isdigit() and int(text) < 1000


def parse_line(line: str) -> tuple[str, list[float | None]] | None:
    """解析一行报表 → ``(科目名, [各列金额])``；不是科目行则返回 ``None``。

    年报三表首列常是**附注号**（附注列），识别到就丢掉，只保留金额列，
    因此返回的金额按「本期、上期」顺序排列（供 :func:`parse_statement` 使用）。
    """
    body = strip_ordinal(line)
    match = _NUMBER_PATTERN.search(body)
    if match is None:
        return None
    subject = normalize_subject(body[: match.start()])
    if not subject or any(char.isdigit() for char in subject):
        return None
    tokens = _NUMBER_PATTERN.findall(body[match.start() :])
    if len(tokens) >= 2 and _is_note_column(tokens[0]):
        tokens = tokens[1:]
    return subject, [parse_amount(token) for token in tokens]


def _in_summary_table(text: str, position: int) -> bool:
    """该位置所在**行**是否属于「摘要 / 簡明 / 概要」表。

    A 股公告标题里的「摘要」已由 :func:`official_src.report_period_of_title` 挡住，
    但**正文里**的概要表（港股「簡明綜合全面收益表」= 五年财务概要）必须在这里挡 ——
    否则会把概要当合并报表，数值看着"合理"却来自错误的表（D-24 实测）。
    """
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    line = text[line_start : line_end if line_end != -1 else len(text)]
    return any(marker in line for marker in SUMMARY_MARKERS)


def split_statements(text: str) -> dict[str, str]:
    """按表标题切分正文 → ``{kind: 正文片段}``（同名多张时优先**合并**口径）。"""
    spans: list[dict[str, Any]] = []
    for kind, titles in STATEMENT_TITLES.items():
        for title in titles:
            for match in re.finditer(re.escape(title), text):
                if _in_summary_table(text, match.start()):
                    continue  # 「簡明／摘要／概要」表不作为数据来源
                spans.append(
                    {"start": match.start(), "end": match.end(), "kind": kind, "title": title}
                )
    # 短标题是长标题的子串（``利润表`` ⊂ ``合并利润表``），会把片段切空 → 只保留最长匹配
    spans = [
        span
        for span in spans
        if not any(
            other is not span
            and other["start"] <= span["start"]
            and span["end"] <= other["end"]
            and other["end"] - other["start"] > span["end"] - span["start"]
            for other in spans
        )
    ]
    spans.sort(key=lambda span: span["start"])

    # 同名标题常出现多次：**目录页**、正文交叉引用、附注里的提及、以及真正的报表。
    # 判据（2026-10-14 实测两轮才定下来，见《计划完成情况》D-24）：
    #   ① 这段能不能**真解析成该表**（必填科目是否齐全）—— 语义判据，最可靠；
    #   ② 其次看金额数量；③ 最后才比"合并口径"。
    # 只靠"金额最多"会输给财务报**附注**（附注里数字更多）；只靠"先到先得"会选中
    # **目录页**（腾讯年报目录把三个标题排在相邻三行，选出来只有 5 个字符）。
    chosen: dict[str, tuple[tuple[int, int, bool], dict[str, Any]]] = {}
    for index, span in enumerate(spans):
        stop = spans[index + 1]["start"] if index + 1 < len(spans) else len(text)
        sample = text[span["end"] : stop][:12000]
        missing = missing_subjects(span["kind"], parse_statement(sample, kind=span["kind"]))
        score = (
            len(REQUIRED_SUBJECTS[span["kind"]]) - len(missing),
            len(_NUMBER_PATTERN.findall(sample)),
            _is_merged_title(span["title"]),
        )
        previous = chosen.get(span["kind"])
        if previous is None or score > previous[0]:
            chosen[span["kind"]] = (score, {**span, "stop": stop})
    return {kind: text[item[1]["end"] : item[1]["stop"]] for kind, item in chosen.items()}


def _is_merged_title(title: str) -> bool:
    """是否「合并口径」标题（``合并`` / ``合併`` / ``綜合``）——同名多张时优先取它。"""
    return any(marker in title for marker in MERGED_MARKERS)


def _candidate_lines(text: str, *, lookahead: int = 3) -> list[str]:
    """匹配用的候选行：**原行 + 「本行 + 后 N 行」的各种拼接**。

    两种真实版式都靠它兜住（2026-10-14 实测）：
    - A 股：标签被换行切断（``归属于母公司股东权益合`` / ``计 221,338,…``）；
    - 港股（pymupdf 抽取）：**一个单元格一行**，标签与金额差好几行
      （``收入`` / ``751,766`` / ``660,257``）→ 只看相邻两行仍然匹配不上。

    ``lookahead`` 越大覆盖越好、也越慢；3 行足够覆盖"标签 + 本期 + 上期"。

    **注意**：拼接会让 subject 变长（如 ``收入 －中國內地``），配合
    :data:`EXACT_ONLY_CANDIDATES` 才不会把附注里的分类行误当科目。
    """
    raw = [line for line in (item.strip() for item in text.splitlines()) if line]
    out = list(raw)
    for index in range(len(raw)):
        for step in range(1, lookahead + 1):
            if index + step < len(raw):
                out.append(" ".join(raw[index : index + step + 1]))
    return out


def parse_statement(text: str, *, kind: str) -> dict[str, list[float | None]]:
    """在一张表里按科目名取**各列**金额（第 1 列=本期，第 2 列=上期）。"""
    lines = _candidate_lines(text)
    values: dict[str, list[float | None]] = {}
    for field_name, candidates in SUBJECTS[kind].items():
        found: list[float | None] | None = None
        for candidate in candidates:
            exact: list[float | None] | None = None
            loose: list[float | None] | None = None
            for line in lines:
                parsed = parse_line(line)
                if parsed is None:
                    continue
                subject, amounts = parsed
                if not amounts:
                    continue
                if subject == candidate and exact is None:
                    exact = amounts
                elif (
                    loose is None
                    and candidate not in EXACT_ONLY_CANDIDATES
                    and candidate in subject
                ):
                    loose = amounts
            found = exact if exact is not None else loose
            if found is not None:
                break
        values[field_name] = found if found is not None else [None]
    return values


def missing_subjects(kind: str, values: dict[str, list[float | None]]) -> list[str]:
    """该表缺失的必填科目（D-11：缺失即报错，不填充）。以**本期列**判定。"""
    return [
        name for name in REQUIRED_SUBJECTS[kind] if not values.get(name) or values[name][0] is None
    ]


def _to_billion(value: float | None, unit: float) -> float | None:
    """元 → 亿元，保留 2 位小数（家法 R10）；``None`` 保持 ``None``（R11）。"""
    if value is None:
        return None
    return round(value * unit / 1e8, config.AMOUNT_DECIMALS)


def build_report_rows(
    code: str,
    text: str,
    *,
    report_period: str,
    report_type: str = "annual",
    source: str = "official",
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    """年报正文 → 标准层 ``financial_report`` 行（**本期 + 上期**两行）。

    年报三表通常并列「本期 / 上期」两列，因此一份年报能产出两个报告期
    （上期 = 本期 - 1 年）；单位统一换算为**亿元**。

    :raises DataSourceError: 定位不到三表、或必填科目缺失（消息里列出缺哪些）。
    """
    statements = split_statements(text)
    if not statements:
        raise DataSourceError(
            f"未在年报正文中定位到三表（{code} {report_period}）", source=source, code=code
        )

    parsed: dict[str, dict[str, list[float | None]]] = {}
    units: dict[str, float] = {}
    ordered_kinds = [kind for kind in STATEMENT_TITLES if kind in statements]
    if not ordered_kinds:
        raise DataSourceError(
            f"三表内容为空（{code} {report_period}）：可能是扫描件 PDF，需要 OCR",
            source=source,
            code=code,
        )
    for kind in ordered_kinds:
        fragment = statements[kind]
        # 单位**按单张表**识别（不能传整本）：港股的单位声明在报表页头部，从整本第 1 页
        # 往后找只会读到封面 → 默认成「元」→ 结果整体差 100 倍（D-24 实测教训）
        units[kind] = detect_unit_multiplier(fragment)
        values = parse_statement(fragment, kind=kind)
        missing = missing_subjects(kind, values)
        if missing:
            raise DataSourceError(
                f"{kind} 表必填科目缺失：{missing}（{code} {report_period}）——不猜测填充",
                source=source,
                code=code,
            )
        parsed[kind] = values

    year, month, day = report_period.split("-")
    periods = (report_period, f"{int(year) - 1}-{month}-{day}")

    rows: list[dict[str, Any]] = []
    for column, period in enumerate(periods):

        def cell(kind: str, field_name: str, column: int = column) -> float | None:
            table = parsed.get(kind)
            if not table:
                return None
            amounts = table.get(field_name) or []
            amount = amounts[column] if column < len(amounts) else None
            return _to_billion(amount, units.get(kind, 1.0))

        revenue = cell("income", "revenue")
        cost = cell("income", "cost")
        gross = None
        if revenue is not None and cost is not None:
            gross = round(revenue - cost, config.AMOUNT_DECIMALS)

        rows.append(
            {
                "code": code,
                "report_period": period,
                "report_type": report_type,
                "is_cumulative": report_type != "annual",
                "revenue": revenue,
                "gross_profit": gross,  # 报表未必直接给毛利 → 用 revenue − cost 反算
                "net_profit": cell("income", "net_profit"),
                "total_assets": cell("balance", "total_assets"),
                "total_liabilities": cell("balance", "total_liabilities"),
                "equity": cell("balance", "equity"),
                "cash": cell("balance", "cash"),
                "op_cashflow": cell("cashflow", "op_cashflow"),
                "icf": cell("cashflow", "icf"),
                "fcf": cell("cashflow", "fcf"),
                "effective_date": None,  # 由 M1-4 清洗阶段统一补齐（家法 R9）
                "currency": _detect_currency_of(ordered_kinds, statements),  # D-24
                "source": source,
                "fetched_at": fetched_at,
            }
        )
    return rows


def _detect_currency_of(kinds: list[str], statements: dict[str, str]) -> str | None:
    """三表里第一个能识别出币种的（认「币种+单位」那种可靠写法）；都识别不出 → ``None``。"""
    for kind in kinds:
        found = detect_currency(statements[kind])
        if found is not None:
            return found
    return None


__all__ = [
    "BLANK_TOKENS",
    "CURRENCY_ALIASES",
    "EXACT_ONLY_CANDIDATES",
    "MERGED_MARKERS",
    "REQUIRED_SUBJECTS",
    "STATEMENT_TITLES",
    "SUBJECTS",
    "SUMMARY_MARKERS",
    "UNIT_FACTORS",
    "build_report_rows",
    "detect_currency",
    "detect_unit_multiplier",
    "extract_text",
    "missing_subjects",
    "normalize_subject",
    "parse_amount",
    "parse_line",
    "parse_statement",
    "split_statements",
]
