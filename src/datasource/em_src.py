"""东财财报源（M5-1 / D-55，2026-11-04）—— A 股 + 港股各一份映射。

**平权可选源之一**：A 股默认 ``em``、港股默认 ``em``；失败不自动换源（见 ADR-015）。
本类只覆写 ``financials``，其余接口（股票列表 / 行情 / 快照 / 行业树）复用
:class:`AkShareSource` 的新浪系实现 —— 这些是全市场级接口，与财报选源无关。

两种返回形态：
- **A 股**：``stock_*_by_yearly_em`` 三张**宽表**（319 / 203 / 254 列，英文大写字段、
  单位元），28 期（1998–2025）；``TOTAL_PARENT_EQUITY`` 归母权益直给。
- **港股**：``stock_financial_hk_report_em`` 三张**长表**（``STD_ITEM_NAME`` + ``AMOUNT``，
  单位元）。**科目名跨股不统一**（00700「股东权益」= 归母，00939「归属于母公司股东权益」），
  必须关键词组兜底。实测该接口**不含 CURRENCY 列**（11 列），故 ``currency=None`` 不猜；
  AMOUNT 为**原始币种**金额，与腾讯源折算后的口径一致，可直接混榜比较比率。

单位口径：元 → 亿元、2 位小数；缺失一律 ``None``（家法 R11）。
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

from .. import config
from ..common import effective_date_of, round_half_up
from .akshare_src import (
    _REPORT_SLUG,
    REPORT_BALANCE,
    REPORT_CASHFLOW,
    REPORT_INCOME,
    AkShareSource,
)

logger = logging.getLogger(__name__)

__all__ = ["EastMoneySource", "map_em_a_financials", "map_em_hk_financials"]

_EM_A_TABLES: dict[str, str] = {
    REPORT_BALANCE: "stock_balance_sheet_by_yearly_em",
    REPORT_INCOME: "stock_profit_sheet_by_yearly_em",
    REPORT_CASHFLOW: "stock_cash_flow_sheet_by_yearly_em",
}

# 东财 A 股宽表字段（英文大写，单位元）。同一字段给出多个候选，按序取首个有效值。
_EM_A_FIELDS: dict[str, tuple[str, ...]] = {
    "revenue": ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
    "cost": ("OPERATE_COST",),
    "net_profit": ("PARENT_NETPROFIT", "NETPROFIT"),
    "total_assets": ("TOTAL_ASSETS",),
    "total_liabilities": ("TOTAL_LIABILITIES",),
    "equity": ("TOTAL_PARENT_EQUITY", "TOTAL_EQUITY"),
    "cash": ("MONETARYFUNDS",),
    "op_cashflow": ("NETCASH_OPERATE",),
    "icf": ("NETCASH_INVEST",),
    "fcf": ("NETCASH_FINANCE",),
}

# 东财港股长表科目名。**顺序即优先级**：把「归属于母公司……」放在「股东权益」之前，
# 否则 00939 会误取含少数股东权益的总额（跨股不统一的坑，见 05 §3.5.2）。
_EM_HK_ITEMS: dict[str, tuple[str, ...]] = {
    "revenue": ("营业额", "经营收入总额", "营运收入", "营业收入", "收入总额"),
    "cost": ("营业成本", "销售成本", "经营成本"),
    "net_profit": (
        "股东应占溢利",
        "归属于母公司股东的净利润",
        "归属母公司所有者净利润",
        "本公司股东应占溢利",
    ),
    "total_assets": (
        "资产总计",
        "总资产",
        "总权益及总负债",
        "权益及负债合计",
        "总权益及非流动负债",
    ),
    "total_liabilities": ("负债合计", "负债总额", "总负债"),
    "equity": ("归属于母公司股东权益", "归属母公司股东权益", "股东权益", "本公司股东应占权益"),
    "cash": ("现金及等价物", "库存现金及短期资金", "现金及现金等价物", "期末现金"),
    "op_cashflow": ("经营业务现金净额", "经营活动产生现金净流量", "经营活动现金流量净额"),
    "icf": ("投资业务现金净额", "投资活动产生的现金流量净额"),
    "fcf": ("筹资业务现金净额", "融资活动现金净额", "筹资活动产生的现金流量净额"),
}


def _amount_yuan(value: Any) -> float | None:
    """元 → 亿元（2 位小数）；空 / NaN / 非数值 → ``None``。"""
    if value is None or value == "" or value is False:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return round_half_up(num / 1e8, config.AMOUNT_DECIMALS)


def _pick(row: dict[str, Any], *columns: str) -> float | None:
    for col in columns:
        value = _amount_yuan(row.get(col))
        if value is not None:
            return value
    return None


def _period_of(row: dict[str, Any]) -> str | None:
    """东财 ``REPORT_DATE`` = ``2025-12-31 00:00:00`` → ``2025-12-31``。"""
    raw = str(row.get("REPORT_DATE") or "")
    if len(raw) < 10:
        return None
    out = raw[:10]
    return out if out[4] == "-" and out[7] == "-" else None


def _stamp(fetched_at: str | None) -> str:
    return fetched_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(
    code: str,
    period: str,
    values: dict[str, float | None],
    stamp: str,
    source: str,
    currency: str | None,
) -> dict[str, Any]:
    revenue = values.get("revenue")
    cost = values.get("cost")
    gross = (
        round_half_up(revenue - cost, config.AMOUNT_DECIMALS)
        if revenue is not None and cost is not None
        else None
    )
    return {
        "code": code,
        "report_period": period,
        "report_type": "annual",
        "is_cumulative": False,
        "revenue": revenue,
        "gross_profit": gross,
        "net_profit": values.get("net_profit"),
        "total_assets": values.get("total_assets"),
        "total_liabilities": values.get("total_liabilities"),
        "equity": values.get("equity"),
        "cash": values.get("cash"),
        "op_cashflow": values.get("op_cashflow"),
        "icf": values.get("icf"),
        "fcf": values.get("fcf"),
        "effective_date": effective_date_of(period),
        "currency": currency,
        "source": source,
        "fetched_at": stamp,
    }


def map_em_a_financials(
    code: str,
    tables: dict[str, list[dict[str, Any]]],
    *,
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    """东财 A 股三张宽表 → 标准层 ``financial_report`` 行（近 ``config.YEARS`` 个完整财年）。"""
    by_period: dict[str, dict[str, dict[str, Any]]] = {}
    for report, rows in tables.items():
        for row in rows:
            period = _period_of(row)
            if period and period.endswith("-12-31"):  # 只取年报（by_yearly 本就只有年报）
                by_period.setdefault(period, {})[report] = row

    stamp = _stamp(fetched_at)
    out: list[dict[str, Any]] = []
    for period in sorted(by_period)[-config.YEARS :]:
        merged: dict[str, Any] = {}
        for report_rows in by_period[period].values():
            merged.update(report_rows)
        values = {key: _pick(merged, *cols) for key, cols in _EM_A_FIELDS.items()}
        out.append(_row(code, period, values, stamp, "em", "CNY"))
    return out


def _hk_lookup(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """港股长表 → ``{报告期: {科目名: AMOUNT}}``。"""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        period = _period_of(row)
        name = str(row.get("STD_ITEM_NAME") or "").strip()
        if period and name:
            out.setdefault(period, {})[name] = row.get("AMOUNT")
    return out


def map_em_hk_financials(
    code: str,
    tables: dict[str, list[dict[str, Any]]],
    *,
    fetched_at: str | None = None,
    indicators: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """东财港股三张长表 → 标准层行；科目名跨股不统一，按 ``_EM_HK_ITEMS`` 关键词组兜底。

    ``indicators`` 是「财务分析主要指标」的行（D-58）：三表**没有毛利科目**，
    用其中的 ``GROSS_PROFIT_RATIO`` 反算 ``gross_profit = revenue × 比率``，
    这样评分层的 ``gross_profit ÷ revenue`` 正好等于该比率（口径一致）。
    """
    lookups = {report: _hk_lookup(rows) for report, rows in tables.items()}
    periods = set().union(*(set(lookup) for lookup in lookups.values())) if lookups else set()
    margin_by_period = _hk_margin_ratios(indicators or [])
    stamp = _stamp(fetched_at)
    out: list[dict[str, Any]] = []
    for period in sorted(periods)[-config.YEARS :]:
        merged: dict[str, Any] = {}
        for lookup in lookups.values():
            merged.update(lookup.get(period, {}))
        values: dict[str, float | None] = {}
        for key, candidates in _EM_HK_ITEMS.items():
            values[key] = next(
                (v for name in candidates if (v := _amount_yuan(merged.get(name))) is not None),
                None,
            )
        # 负债缺列时用「资产 − 总权益」兜底（总权益含少数股东，仍优于留空）
        if values.get("total_liabilities") is None:
            assets = values.get("total_assets")
            total_equity = next(
                (
                    v
                    for name in ("股东权益合计", "总权益", "权益合计")
                    if (v := _amount_yuan(merged.get(name))) is not None
                ),
                None,
            )
            if assets is not None and total_equity is not None:
                values["total_liabilities"] = round_half_up(
                    assets - total_equity, config.AMOUNT_DECIMALS
                )
        # 毛利：`_row` 用 `revenue − cost` 自算，但腾讯等票的「销售成本」科目名对不上 →
        # 为 None；此时用指标接口的毛利率反算（D-58），保证 gross_margin 有值。
        row = _row(code, period, values, stamp, "em", None)
        if row.get("gross_profit") is None:
            ratio = margin_by_period.get(period)
            revenue = row.get("revenue")
            if ratio is not None and revenue:
                row["gross_profit"] = round_half_up(
                    revenue * ratio / 100.0, config.AMOUNT_DECIMALS
                )
        out.append(row)
    return out


def _hk_margin_ratios(rows: list[dict[str, Any]]) -> dict[str, float]:
    """「财务分析主要指标」→ ``{报告期: 毛利率百分数}``（如 ``56.21``）。"""
    ratios: dict[str, float] = {}
    for row in rows:
        period = str(row.get("REPORT_DATE") or "")[:10]
        raw = row.get("GROSS_PROFIT_RATIO")
        if not period or raw is None:
            continue
        try:
            ratios[period] = float(raw)
        except (TypeError, ValueError):
            continue
    return ratios


class EastMoneySource(AkShareSource):
    """东财财报源：``name = "em"``；A 股 / 港股均覆盖（M5-1 默认源）。"""

    name = "em"

    def financials(self, code: str) -> list[dict[str, Any]]:
        if code.upper().startswith("HK"):
            return self._hk_financials(code)
        return self._a_financials(code)

    def _a_financials(self, code: str) -> list[dict[str, Any]]:
        tables = {
            report: self._fetch(
                f"financial_em_a_{code}_{_REPORT_SLUG[report]}",
                getattr(self.ak, endpoint),
                what=f"东财A股财报（{code} / {report}）",
                code=code,
                timeout=config.FETCH_TIMEOUT_MEDIUM_SECONDS,
                symbol=code,
            )
            for report, endpoint in _EM_A_TABLES.items()
        }
        return map_em_a_financials(code, tables)

    def _hk_financials(self, code: str) -> list[dict[str, Any]]:
        stock = code[2:] if code.upper().startswith("HK") else code
        tables = {
            report: self._fetch(
                f"financial_em_hk_{code}_{_REPORT_SLUG[report]}",
                self.ak.stock_financial_hk_report_em,
                what=f"东财港股财报（{code} / {report}）",
                code=code,
                timeout=config.FETCH_TIMEOUT_MEDIUM_SECONDS,
                stock=stock,
                symbol=report,
                indicator="年度",
            )
            for report in (REPORT_INCOME, REPORT_BALANCE, REPORT_CASHFLOW)
        }
        return map_em_hk_financials(code, tables, indicators=self._hk_indicators(code, stock))

    def _hk_indicators(self, code: str, stock: str) -> list[dict[str, Any]]:
        """港股「财务分析主要指标」（D-58）：补三表缺的毛利率。

        东财港股三表没有毛利科目 → 评分器的 ``gross_margin`` 恒缺失 → 港股不入主榜。
        失败只降级（毛利率留空），不阻断三表主体数据。
        """
        try:
            return self._fetch(
                f"hk_indicator_{code}",
                self.ak.stock_financial_hk_analysis_indicator_em,
                what=f"东财港股指标（{code}）",
                code=code,
                timeout=config.FETCH_TIMEOUT_MEDIUM_SECONDS,
                symbol=stock,
                indicator="年度",
            )
        except Exception as exc:  # noqa: BLE001  增强数据，失败不阻断
            logger.warning("港股指标接口不可用，%s 的毛利率将留空：%s", code, exc)
            return []
