"""数据源抽象接口的契约测试（任务 M1-1 验收：接口文档齐全）。

思路：把《数据字典》§3 的**必填 / 可选字段清单**固化成断言。任何实现（M1-2 AkShare、
M1-3 官方源、M1-6 fixtures 回退）少返回一个必填字段，都应该在这里被拦住——
不必等到 M1-4 清洗阶段才发现。
"""

from __future__ import annotations

from typing import Any

import pytest

from src import config
from src.datasource import base


# ── 异常契约 ──────────────────────────────────────────────────────────────
def test_error_is_identifiable_runtime_error() -> None:
    """失败必须是可识别异常（家法：上层靠它决定回退），而不是静默返回空列表。"""
    assert issubclass(base.DataSourceError, RuntimeError)
    err = base.DataSourceError("timeout", source="akshare", code="SH600900")
    assert err.source == "akshare"
    assert err.code == "SH600900"
    assert "timeout" in str(err)


# ── 字段契约：与《数据字典》§3 一致 ────────────────────────────────────────
def test_stock_field_contract() -> None:
    assert base.STOCK_REQUIRED_FIELDS == (
        "code",
        "name",
        "market",
        "exchange",
        "l1_code",
        "l1_name",
        "l2_code",
        "l2_name",
        "industry_std",
        "status",
        "is_financial",
        "updated_at",
    )
    assert base.STOCK_OPTIONAL_FIELDS == ("listed_at",)


def test_financial_field_contract() -> None:
    assert base.FINANCIAL_REQUIRED_FIELDS == (
        "code",
        "report_period",
        "report_type",
        "is_cumulative",
        "revenue",
        "net_profit",
        "total_assets",
        "equity",
        "op_cashflow",
        "effective_date",
        "source",
        "fetched_at",
    )
    # 这些是「可选」——空值用 None（家法 R11），实现不得用 0 顶替
    assert base.FINANCIAL_OPTIONAL_FIELDS == (
        "eps",
        "gross_profit",
        "total_liabilities",
        "cash",
        "icf",
        "fcf",
        # 报表币种（CNY / HKD / USD）：金额单位是"亿元"但币种不能混 —— 2026-10-14 加（D-24）
        "currency",
    )


def test_price_field_contract() -> None:
    assert base.PRICE_REQUIRED_FIELDS == (
        "code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "adj_factor",
        "close_qfq",
    )
    assert base.PRICE_OPTIONAL_FIELDS == ("volume",)


def test_industry_field_contract() -> None:
    assert base.INDUSTRY_REQUIRED_FIELDS == (
        "standard",
        "version",
        "level",
        "node_code",
        "node_name",
    )
    assert base.INDUSTRY_OPTIONAL_FIELDS == ("parent_code", "member_count")


def test_field_contracts_never_overlap() -> None:
    """必填与可选不得重叠，否则"是否必填"这件事就没有唯一答案。"""
    for required, optional in (
        (base.STOCK_REQUIRED_FIELDS, base.STOCK_OPTIONAL_FIELDS),
        (base.FINANCIAL_REQUIRED_FIELDS, base.FINANCIAL_OPTIONAL_FIELDS),
        (base.PRICE_REQUIRED_FIELDS, base.PRICE_OPTIONAL_FIELDS),
        (base.INDUSTRY_REQUIRED_FIELDS, base.INDUSTRY_OPTIONAL_FIELDS),
    ):
        assert set(required) & set(optional) == set()
        assert len(set(required)) == len(required)
        assert len(set(optional)) == len(optional)


# ── 参数口径 ──────────────────────────────────────────────────────────────
def test_market_and_adjust_vocabulary_comes_from_config() -> None:
    """市场与复权取值只有一个来源（config），接口层不得另行硬编码。"""
    assert base.MARKETS == config.MARKETS == ("A", "HK")
    assert base.ADJUST_MODES == config.ADJUST_MODES == ("qfq", "hfq", "raw")
    assert base.DEFAULT_ADJUST == config.DEFAULT_ADJUST == "qfq"  # 家法 R8：默认前复权


# ── Protocol 契约 ─────────────────────────────────────────────────────────
class _CompleteStub:
    """满足接口的最小实现（仅用于验证 Protocol 能识别）。"""

    name = "stub"

    def list_stocks(self, market: str) -> list[dict[str, Any]]:
        return []

    def financials(self, code: str) -> list[dict[str, Any]]:
        return []

    def price_daily(self, code: str, adjust: str = "qfq") -> list[dict[str, Any]]:
        return []

    def industry_tree(self, market: str) -> list[dict[str, Any]]:
        return []

    def market_snapshot(self, market: str) -> list[dict[str, Any]]:
        return []

    def health_check(self) -> bool:
        return True


class _IncompleteStub:
    """缺 4 个方法——不得被判定为合规实现。"""

    name = "incomplete"

    def list_stocks(self, market: str) -> list[dict[str, Any]]:
        return []


def test_protocol_accepts_complete_implementation() -> None:
    assert isinstance(_CompleteStub(), base.DataSource)


def test_protocol_rejects_incomplete_implementation() -> None:
    assert not isinstance(_IncompleteStub(), base.DataSource)


@pytest.mark.parametrize("method", ["list_stocks", "financials", "price_daily", "industry_tree"])
def test_protocol_declares_all_spec_methods(method: str) -> None:
    """方法名以《数据源接入规格》§2 为准（计划 M1-1 的旧写法已对齐）。"""
    assert hasattr(base.DataSource, method)
    assert method in _CompleteStub.__dict__


def test_protocol_declares_health_check() -> None:
    assert hasattr(base.DataSource, "health_check")
