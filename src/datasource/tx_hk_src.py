"""腾讯港股财报源（M5-1 / D-53，2026-11-04）—— 港股可选源之一（``tx``）。

接口（非公开页面 API，无 SLA；旧接口 ``HkInfo/getFinReport`` 已 offline）：

    GET https://proxy.finance.qq.com/ifzqgtimg/stock/corp/hkcwbb/detail
        ?num=10&_appName=android&type={zhsy|zcfz|xjll}&rttype=0&symbol=hk00700
    Header: Referer: https://gu.qq.com/hk00700/cwbb

``rttype``：0=年报 / 1=中报 / 2=一季报 / 3=三季报 / all（由页面 bundle 反解，无文档）。
响应 ``data.data`` = **每期一个二维表**，每行 ``[[科目名, {样式}], [金额串, {样式}], 同比]``：
金额形如 ``"1,561.54亿元"`` / ``"--"``；表尾固定四行给出 **显示币种 / 原始币种 /
转换汇率 / 会计准则**。

**关键口径（D-53）**：金额是**显示币种（港币）**口径，必须 ``÷ 转换汇率`` 还原为原始币种，
否则港股与 A 股混榜会系统性高估约 12%。每期汇率不同（实测 1.11~1.20），逐期折算。

**腾讯 A 股无可用财报接口**（旧页 302、bundle 无三表 API、akshare 无封装）→ 本源只服务港股，
注册表不会把它挂到 A 股；误传 A 股代码时明确抛错而不是静默返回错数据。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import requests

from .. import config
from ..common import effective_date_of, round_half_up
from .akshare_src import AkShareSource
from .base import DataSourceError

__all__ = ["TencentHKSource", "map_tx_hk_financials", "parse_tx_amount"]

_URL = "https://proxy.finance.qq.com/ifzqgtimg/stock/corp/hkcwbb/detail"
_TYPES = {"zhsy": "综合损益表", "zcfz": "资产负债表", "xjll": "现金流量表"}

# 科目名 → 内部字段；顺序即优先级（腾讯港股科目名相对统一，仍留备选）
_TX_ITEMS: dict[str, tuple[str, ...]] = {
    "revenue": ("营业收入", "营业额", "经营收入总额"),
    "cost": ("营业成本", "销售成本"),
    "net_profit": ("归属母公司所有者净利润", "归属母公司股东净利润", "股东应占溢利"),
    "total_assets": ("资产总计", "资产合计", "总资产"),
    "total_liabilities": ("负债合计", "负债总额", "总负债"),
    "equity": ("归属母公司股东权益", "归属于母公司股东权益", "归属母公司所有者权益合计"),
    "cash": ("其中:现金及现金等价物", "现金及短期投资", "期末现金及现金等价物余额"),
    "op_cashflow": ("经营活动产生现金净流量", "经营活动产生的现金流量净额"),
    "icf": ("投资活动产生的现金流量净额",),
    "fcf": ("筹资活动产生的现金流量净额", "融资活动产生的现金流量净额"),
}

_CURRENCY = {"人民币": "CNY", "港币": "HKD", "港元": "HKD", "美元": "USD", "美元(US$)": "USD"}
_AMOUNT_RE = re.compile(r"^(-?[\d,]+(?:\.\d+)?)\s*(万亿|亿|万)?元?$")
_UNIT_SCALE = {"万亿": 1e4, "亿": 1.0, "万": 1e-4, None: 1e-8}


def parse_tx_amount(raw: Any) -> float | None:
    """腾讯金额串 → **亿元**浮点数（``"1,561.54亿元"`` → 1561.54；``"--"`` → None）。"""
    if raw is None or raw is False:
        return None
    text = str(raw).strip().replace(",", "").replace("，", "")
    if not text or text in {"--", "-", "false", "False", "None", "nan"}:
        return None
    match = _AMOUNT_RE.match(text)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value * _UNIT_SCALE[match.group(2)]


def _cell(row: Any, index: int) -> str:
    """二维表单元格 → 文本（``[['科目', {...}], ['金额', {...}]]``）。"""
    if not isinstance(row, list | tuple) or len(row) <= index:
        return ""
    cell = row[index]
    if isinstance(cell, list | tuple) and cell:
        return str(cell[0]).strip()
    if isinstance(cell, str):
        return cell.strip()
    return ""


def _parse_period(rows: list[Any]) -> dict[str, Any]:
    """把一期二维表解析成 ``{period, fields, currency, rate}``。"""
    fields: dict[str, str] = {}
    meta: dict[str, str] = {}
    period = ""
    for row in rows:
        name, value = _cell(row, 0), _cell(row, 1)
        if not name:
            # 表头：第二列是报告期（"20251231"），样式字典里带 fiscalYear
            if not period and value and re.fullmatch(r"\d{8}", value):
                period = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
            continue
        if name in {"显示币种", "原始币种", "转换汇率", "会计准则"}:
            meta[name] = value
        elif value:
            fields[name] = value
    # 汇率是纯倍数（1.11），不是金额，不能用 parse_tx_amount（会把无单位数字当「元」）
    try:
        rate = float(meta.get("转换汇率") or 1.0)
    except ValueError:
        rate = 1.0
    return {
        "period": period,
        "fields": fields,
        "currency": _CURRENCY.get(meta.get("原始币种", ""), None),
        "rate": rate if rate > 0 else 1.0,
    }


def _eps_value(value: Any) -> float | None:
    """EPS 是「元/股」，**不经过金额（元→亿元）转换**（D-67）。"""
    if value is None:
        return None
    try:
        return round(float(str(value).replace(",", "").replace("元", "")), 4)
    except (TypeError, ValueError):
        return None


def map_tx_hk_financials(
    code: str,
    periods: list[dict[str, Any]],
    *,
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    """三表按期合并 + 汇率折算 → 标准层 ``financial_report`` 行。"""
    stamp = fetched_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[dict[str, Any]] = []
    for item in periods:
        period = str(item.get("period") or "")
        if len(period) != 10:
            continue
        fields: dict[str, str] = item.get("fields") or {}
        rate = float(item.get("rate") or 1.0)

        # 用默认参数绑定本轮循环变量（B023），避免闭包延迟求值取到最后一期
        def pick(
            *names: str, _fields: dict[str, str] = fields, _rate: float = rate
        ) -> float | None:
            for name in names:
                value = parse_tx_amount(_fields.get(name))
                if value is None:
                    continue
                # 金额从「显示币种」折算回「原始币种」，再统一到亿元
                return round_half_up(value / _rate, config.AMOUNT_DECIMALS)
            return None

        values = {key: pick(*cols) for key, cols in _TX_ITEMS.items()}
        # 营业成本在腾讯口径为负值；统一取绝对值，否则毛利会被算成 revenue + |cost|
        if values.get("cost") is not None:
            values["cost"] = abs(values["cost"])
        # 负债缺列时按「资产 − 股东权益合计」兜底
        if values.get("total_liabilities") is None:
            assets = values.get("total_assets")
            total_equity = pick("股东权益合计", "总权益")
            if assets is not None and total_equity is not None:
                values["total_liabilities"] = round_half_up(
                    assets - total_equity, config.AMOUNT_DECIMALS
                )

        revenue, cost = values.get("revenue"), values.get("cost")
        gross = (
            round_half_up(revenue - cost, config.AMOUNT_DECIMALS)
            if revenue is not None and cost is not None
            else None
        )
        out.append(
            {
                "code": code,
                "report_period": period,
                "report_type": "annual",
                "is_cumulative": False,
                "revenue": revenue,
                "gross_profit": gross,
                "net_profit": values.get("net_profit"),
                # D-67：EPS 是元/股，不能走 pick() 的元→亿元折算
                "eps": _eps_value(
                    (item.get("fields") or {}).get("基本每股收益")
                    or (item.get("fields") or {}).get("摊薄每股收益")
                ),
                "total_assets": values.get("total_assets"),
                "total_liabilities": values.get("total_liabilities"),
                "equity": values.get("equity"),
                "cash": values.get("cash"),
                "op_cashflow": values.get("op_cashflow"),
                "icf": values.get("icf"),
                "fcf": values.get("fcf"),
                "effective_date": effective_date_of(period),
                "currency": item.get("currency"),
                "source": "tx",
                "fetched_at": stamp,
            }
        )
    out.sort(key=lambda r: r["report_period"])
    return out[-config.YEARS :] if config.YEARS else out


class TencentHKSource(AkShareSource):
    """腾讯港股财报源：``name = "tx"``；**只服务港股**（A 股无可用接口）。"""

    name = "tx"

    def financials(self, code: str) -> list[dict[str, Any]]:
        if not code.upper().startswith("HK"):
            raise DataSourceError(
                f"腾讯 A 股无可用的结构化财报接口（{code}）：旧页 302、bundle 无三表 API、"
                "akshare 无封装；请改用 sina / em / ths（D-55 实测否决）",
                source=self.name,
                code=code,
            )
        symbol = code.lower()  # HK00700 → hk00700
        key = f"financial_tx_hk_{code}"
        cached = self._load_cache(key)
        if cached is not None:
            return map_tx_hk_financials(code, cached)

        self._throttle()
        periods: dict[str, dict[str, Any]] = {}
        for rtype, label in _TYPES.items():
            try:
                resp = requests.get(
                    _URL,
                    params={
                        "num": 10,
                        "_appName": "android",
                        "type": rtype,
                        "rttype": 0,  # 年报
                        "symbol": symbol,
                    },
                    headers={
                        "Referer": f"https://gu.qq.com/{symbol}/cwbb",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    },
                    timeout=config.FETCH_TIMEOUT_MEDIUM_SECONDS,
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001  网络/解析失败 → 由上层记为失败
                raise DataSourceError(
                    f"腾讯港股财报请求失败（{code} / {label}）：{type(exc).__name__}: {exc}",
                    source=self.name,
                    code=code,
                ) from exc
            if payload.get("code") != 0:
                raise DataSourceError(
                    f"腾讯港股财报返回异常（{code} / {label}）：code={payload.get('code')}",
                    source=self.name,
                    code=code,
                )
            tables = (payload.get("data") or {}).get("data") or []
            for rows in tables:
                parsed = _parse_period(rows)
                if not parsed["period"]:
                    continue
                slot = periods.setdefault(
                    parsed["period"],
                    {
                        "period": parsed["period"],
                        "fields": {},
                        "currency": parsed["currency"],
                        "rate": parsed["rate"],
                    },
                )
                slot["fields"].update(parsed["fields"])
                if parsed["currency"]:
                    slot["currency"] = parsed["currency"]
                slot["rate"] = parsed["rate"]

        ordered = [periods[p] for p in sorted(periods)]
        self._save_cache(key, ordered)
        return map_tx_hk_financials(code, ordered)
