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
STATEMENT_TITLES: dict[str, tuple[str, ...]] = {
    "income": ("合并利润表", "利润表"),
    "balance": ("合并资产负债表", "资产负债表"),
    "cashflow": ("合并现金流量表", "现金流量表"),
}
MERGED_PREFIX = "合并"

# ── 单位（顺序敏感：先长后短，避免「元」吃掉「万元」）─────────────────────
UNIT_FACTORS: tuple[tuple[str, float], ...] = (
    ("百万元", 1e6),
    ("千元", 1e3),
    ("万元", 1e4),
    ("元", 1.0),
)
UNIT_PATTERN = re.compile(r"单位[：:]\s*(百万元|千元|万元|元)")

# ── 三表科目（标准层字段 → 年报科目名候选，按优先级）──────────────────────
SUBJECTS: dict[str, dict[str, tuple[str, ...]]] = {
    "income": {
        "revenue": ("营业收入", "营业总收入"),
        "cost": ("营业成本", "营业总成本"),
        "net_profit": ("归属于母公司所有者的净利润", "归属于母公司股东的净利润", "净利润"),
    },
    "balance": {
        "total_assets": ("资产总计", "资产合计"),
        "total_liabilities": ("负债合计",),
        "equity": ("归属于母公司所有者权益合计", "归属于母公司股东权益合计", "所有者权益合计"),
        "cash": ("货币资金",),
    },
    "cashflow": {
        "op_cashflow": ("经营活动产生的现金流量净额",),
        "icf": ("投资活动产生的现金流量净额",),
        "fcf": ("筹资活动产生的现金流量净额",),
    },
}

# 每张表必须命中的科目（缺失 → 报错，不猜）
REQUIRED_SUBJECTS: dict[str, tuple[str, ...]] = {
    "income": ("revenue", "net_profit"),
    "balance": ("total_assets", "equity"),
    "cashflow": ("op_cashflow",),
}

# 空值符号（报表里"没有金额"的几种写法）
BLANK_TOKENS = ("", "-", "--", "—", "－", "–", "/", "不适用")
_NUMBER_PATTERN = re.compile(r"\(?\s*-?\d[\d,，]*(?:\.\d+)?\s*\)?|[-—－–]{1,2}")
_SUBJECT_MARKER = re.compile(r"^(其中|加|减|其他)[：:]")
_SUBJECT_ORDER = re.compile(r"^[（(]?[一二三四五六七八九十\d]+[）)、.]\s*")


def extract_text(pdf_path: str | Path, *, reader_factory: Any | None = None) -> str:
    """抽取 PDF 全文（``pypdf`` 延迟导入；测试可注入 fake reader 工厂）。

    返回空串表示"抽不到文本"（扫描件 PDF 需要 OCR，不在本期范围）——
    调用方必须把空串当成**失败**，不能当成"没有数字"。
    """
    if reader_factory is None:
        try:
            from pypdf import PdfReader  # noqa: PLC0415  （刻意延迟导入）
        except ImportError as exc:  # pragma: no cover - 依赖已在 requirements.txt 声明
            raise DataSourceError("pypdf 未安装：pip install pypdf", source="official") from exc
        reader_factory = PdfReader
    reader = reader_factory(str(pdf_path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def detect_unit_multiplier(text: str, *, head_chars: int = 2000) -> float:
    """识别报表单位（``单位：元 / 千元 / 万元 / 百万元``）→ 换算到「元」的乘数。

    找不到时按「元」处理（年报三表正文的默认单位）。
    """
    match = UNIT_PATTERN.search(text[:head_chars])
    return dict(UNIT_FACTORS)[match.group(1)] if match else 1.0


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
    """归一科目名：去掉「其中：」「加：」「一、」「（1）」等前后缀。"""
    text = _SUBJECT_MARKER.sub("", str(name).strip())
    return _SUBJECT_ORDER.sub("", text).strip()


def _is_note_column(token: str) -> bool:
    """判断首个数字是否为「附注」列（年报三表首列的附注号：1、2、3… 这类裸小整数）。"""
    text = token.strip().strip("()")
    return text.isdigit() and int(text) < 1000


def parse_line(line: str) -> tuple[str, list[float | None]] | None:
    """解析一行报表 → ``(科目名, [各列金额])``；不是科目行则返回 ``None``。

    年报三表首列常是**附注号**（附注列），识别到就丢掉，只保留金额列，
    因此返回的金额按「本期、上期」顺序排列（供 :func:`parse_statement` 使用）。
    """
    match = _NUMBER_PATTERN.search(line)
    if match is None:
        return None
    subject = normalize_subject(line[: match.start()])
    if not subject or any(char.isdigit() for char in subject):
        return None
    tokens = _NUMBER_PATTERN.findall(line[match.start() :])
    if len(tokens) >= 2 and _is_note_column(tokens[0]):
        tokens = tokens[1:]
    return subject, [parse_amount(token) for token in tokens]


def split_statements(text: str) -> dict[str, str]:
    """按表标题切分正文 → ``{kind: 正文片段}``（同名多张时优先**合并**口径）。"""
    spans: list[dict[str, Any]] = []
    for kind, titles in STATEMENT_TITLES.items():
        for title in titles:
            for match in re.finditer(re.escape(title), text):
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

    chosen: dict[str, dict[str, Any]] = {}
    for index, span in enumerate(spans):
        stop = spans[index + 1]["start"] if index + 1 < len(spans) else len(text)
        previous = chosen.get(span["kind"])
        is_merged = span["title"].startswith(MERGED_PREFIX)
        if previous is None or (not previous["title"].startswith(MERGED_PREFIX) and is_merged):
            chosen[span["kind"]] = {**span, "stop": stop}
    return {kind: text[span["end"] : span["stop"]] for kind, span in chosen.items()}


def parse_statement(text: str, *, kind: str) -> dict[str, list[float | None]]:
    """在一张表里按科目名取**各列**金额（第 1 列=本期，第 2 列=上期）。"""
    lines = [line for line in (raw.strip() for raw in text.splitlines()) if line]
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
                elif loose is None and candidate in subject:
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

    unit = detect_unit_multiplier(text)
    parsed: dict[str, dict[str, list[float | None]]] = {}
    ordered_kinds = [kind for kind in STATEMENT_TITLES if kind in statements]
    if not ordered_kinds:
        raise DataSourceError(
            f"三表内容为空（{code} {report_period}）：可能是扫描件 PDF，需要 OCR",
            source=source,
            code=code,
        )
    for kind in ordered_kinds:
        values = parse_statement(statements[kind], kind=kind)
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
            return _to_billion(amounts[column] if column < len(amounts) else None, unit)

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
                "source": source,
                "fetched_at": fetched_at,
            }
        )
    return rows


__all__ = [
    "BLANK_TOKENS",
    "MERGED_PREFIX",
    "REQUIRED_SUBJECTS",
    "STATEMENT_TITLES",
    "SUBJECTS",
    "UNIT_FACTORS",
    "build_report_rows",
    "detect_unit_multiplier",
    "extract_text",
    "missing_subjects",
    "normalize_subject",
    "parse_amount",
    "parse_line",
    "parse_statement",
    "split_statements",
]
