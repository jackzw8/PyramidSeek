"""fixtures 数据源的离线测试（任务 M1-6 的第三级回退）。

用真实 `fixtures/financials.json`（12 只样本）验证协议实现，无需网络。
"""

from __future__ import annotations

import json

import pytest

from src import config
from src.datasource import base
from src.datasource.fixtures_src import FixturesSource


@pytest.fixture
def source() -> FixturesSource:
    return FixturesSource()


# ── 载入与健康检查 ────────────────────────────────────────────────────────
def test_health_check_true_for_real_fixtures(source: FixturesSource) -> None:
    assert source.health_check() is True


def test_health_check_false_when_missing(tmp_path) -> None:
    assert FixturesSource(tmp_path / "nope.json").health_check() is False


def test_missing_file_raises_identifiable_error(tmp_path) -> None:
    with pytest.raises(base.DataSourceError, match="无法离线回退"):
        FixturesSource(tmp_path / "nope.json").list_stocks("A")


def test_corrupted_file_raises_identifiable_error(tmp_path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(base.DataSourceError, match="fixtures 读取失败"):
        FixturesSource(broken).list_stocks("A")


# ── 协议实现 ──────────────────────────────────────────────────────────────
def test_list_stocks_strips_nested_financials(source: FixturesSource) -> None:
    rows = source.list_stocks("A")
    assert rows, "A 股样本不应为空"
    for row in rows:
        assert "financials" not in row  # 嵌套财报不能污染标准层
        assert set(base.STOCK_REQUIRED_FIELDS) <= set(row)
    assert {row["market"] for row in rows} == {"A"}


def test_list_stocks_rejects_unknown_market(source: FixturesSource) -> None:
    with pytest.raises(ValueError, match="市场"):
        source.list_stocks("US")


def test_financials_returns_sorted_periods(source: FixturesSource) -> None:
    rows = source.financials("SH600900")
    assert len(rows) >= 3
    periods = [row["report_period"] for row in rows]
    assert periods == sorted(periods)
    for row in rows:
        assert row["code"] == "SH600900"
        assert row["source"] == "fixture"
        assert set(base.FINANCIAL_REQUIRED_FIELDS) <= set(row)


def test_financials_unknown_code_raises(source: FixturesSource) -> None:
    with pytest.raises(base.DataSourceError, match="无此标的"):
        source.financials("SH999999")


def test_price_daily_is_explicitly_unsupported(source: FixturesSource) -> None:
    """fixtures 不含行情 —— 必须明确报错，不能返回空集被当成"没有数据"。"""
    with pytest.raises(base.DataSourceError, match="不含行情数据"):
        source.price_daily("SH600900")


def test_industry_tree_is_derived_from_stock_industry_fields(source: FixturesSource) -> None:
    nodes = source.industry_tree("A")
    levels = {node["level"] for node in nodes}
    assert levels == {1, 2}
    for node in nodes:
        assert node["standard"] == config.INDUSTRY_STANDARD["A"]
        assert node["member_count"] >= 1
        assert "node_code" in node and "node_name" in node
    second_level = [node for node in nodes if node["level"] == 2]
    assert all(node["parent_code"] for node in second_level)


def test_industry_tree_counts_members(source: FixturesSource, tmp_path) -> None:
    """member_count 必须等于该节点下的样本数（自造 2 只同行业样本验证）。"""
    payload = {
        "stocks": [
            {
                "code": "SH600001",
                "name": "甲",
                "market": "A",
                "exchange": "SSE",
                "l1_code": "801160",
                "l1_name": "公用事业",
                "l2_code": "801161",
                "l2_name": "电力",
                "industry_std": "申万2021",
                "status": "normal",
            },
            {
                "code": "SH600002",
                "name": "乙",
                "market": "A",
                "exchange": "SSE",
                "l1_code": "801160",
                "l1_name": "公用事业",
                "l2_code": "801161",
                "l2_name": "电力",
                "industry_std": "申万2021",
                "status": "normal",
            },
        ]
    }
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    nodes = FixturesSource(path).industry_tree("A")
    l2 = [node for node in nodes if node["level"] == 2][0]
    assert l2["member_count"] == 2
    assert l2["parent_code"] == "801160"
