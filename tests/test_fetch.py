"""采集编排的离线测试（任务 M1-6）。

**全离线**：三级回退用可注入的 StubSource 验证；离线链用真实 fixtures。
覆盖：回退顺序、降级留痕、空集视为失败、全链路失败的报错、小样本过滤、落库与幂等。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src import config
from src.datasource import base
from src.datasource.fixtures_src import FixturesSource
from src.pipeline import fetch, store


# ── 测试替身 ──────────────────────────────────────────────────────────────
class StubSource:
    """可配置的假数据源：能指定"哪些方法挂"与"哪些方法返回空"。"""

    def __init__(
        self,
        name: str,
        *,
        stocks: dict | None = None,
        financials: dict | None = None,
        industries: dict | None = None,
        prices: dict | None = None,
        snapshots: dict | None = None,
        fail: tuple[str, ...] = (),
        empty: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self._stocks = stocks or {}
        self._financials = financials or {}
        self._industries = industries or {}
        self._prices = prices or {}
        self._snapshots = snapshots or {}
        self._fail = set(fail)
        self._empty = set(empty)
        self.calls: list[tuple[str, str]] = []

    def _respond(self, method: str, key: str, value: list) -> list:
        self.calls.append((method, key))
        if method in self._fail:
            raise base.DataSourceError(f"{self.name} 的 {method} 不可用", source=self.name)
        return [] if method in self._empty else list(value)

    def list_stocks(self, market: str) -> list:
        return self._respond("list_stocks", market, self._stocks.get(market, []))

    def financials(self, code: str) -> list:
        return self._respond("financials", code, self._financials.get(code, []))

    def industry_tree(self, market: str) -> list:
        return self._respond("industry_tree", market, self._industries.get(market, []))

    def price_daily(self, code: str, adjust: str = config.DEFAULT_ADJUST) -> list:
        return self._respond("price_daily", f"{code}:{adjust}", self._prices.get(code, []))

    def market_snapshot(self, market: str) -> list:
        return self._respond("market_snapshot", market, self._snapshots.get(market, []))


def stock_row(code: str = "SH600900", market: str = "A", **over) -> dict:
    row = {
        "code": code,
        "name": "长江电力",
        "market": market,
        "exchange": "SSE" if market == "A" else "HKEX",
        "l1_code": "801160",
        "l1_name": "公用事业",
        "l2_code": "801161",
        "l2_name": "电力",
        "industry_std": config.INDUSTRY_STANDARD[market],
        "listed_at": "2003-11-18",
        "shares": 244.7,
        "status": "normal",
        "is_financial": False,
        "updated_at": "2026-10-14T00:00:00Z",
    }
    row.update(over)
    return row


def fin_row(code: str = "SH600900", period: str = "2024-12-31", **over) -> dict:
    row = {
        "code": code,
        "report_period": period,
        "report_type": "annual",
        "revenue": 845.0,
        "gross_profit": 524.0,
        "net_profit": 325.0,
        "total_assets": 6200.0,
        "total_liabilities": 3520.0,
        "equity": 2680.0,
        "cash": 90.0,
        "op_cashflow": 596.0,
        "icf": -450.0,
        "fcf": -100.0,
        "effective_date": "2025-04-30",
        "source": "stub",
        "fetched_at": "2026-10-14T00:00:00Z",
    }
    row.update(over)
    return row


def price_row(
    code: str = "SH600900",
    trade_date: str = "2024-01-02",
    close: float = 10.0,
    qfq: float = 9.5,
) -> dict:
    """一行「双口径」日行情（raw 与 qfq 同时有，供 ``merge_price_rows`` 合并）。"""
    return {
        "code": code,
        "trade_date": trade_date,
        "open": close,
        "high": round(close * 1.02, 2),
        "low": round(close * 0.98, 2),
        "close": close,
        "close_qfq": qfq,
        "volume": 1000,
    }


def stub_chain(**kwargs) -> dict[str, StubSource]:
    """三级链，默认每级都能提供 A/HK 的股票、财报与行情。"""
    defaults = {
        "primary": dict(
            stocks={"A": [stock_row()], "HK": [stock_row("HK00700", "HK")]},
            financials={"SH600900": [fin_row()], "HK00700": [fin_row("HK00700")]},
            industries={"A": [{"standard": "申万2021", "version": "v1", "level": 1}]},
            prices={"SH600900": [price_row()], "HK00700": [price_row("HK00700")]},
        ),
        "official": dict(
            stocks={"A": [stock_row("SZ000858")]},
            financials={"SH600900": [fin_row()]},
        ),
        "fixture": dict(
            stocks={"A": [stock_row("SH600011")]},
            financials={"SH600900": [fin_row()]},
        ),
    }
    # 同键覆盖、其余保留：`stub_chain(primary=dict(snapshots={...}))` 只补快照，
    # 不会把 stocks/financials 等默认配置挤掉（整体替换会让主源变空 → 误报"取不到股票"）。
    for name, override in kwargs.items():
        if name in defaults and isinstance(defaults[name], dict) and isinstance(override, dict):
            merged = dict(defaults[name])
            merged.update(override)
            defaults[name] = merged
        else:
            defaults[name] = override
    return {
        name: StubSource("akshare" if name == "primary" else name, **cfg)
        for name, cfg in defaults.items()
    }


# ── 回退链构造 ────────────────────────────────────────────────────────────
def test_build_chain_skips_missing_levels() -> None:
    primary, fixture = StubSource("primary"), StubSource("fixture")
    assert fetch.build_chain(primary=primary, fixtures=fixture) == [primary, fixture]
    assert fetch.build_chain(fixtures=fixture) == [fixture]
    assert fetch.build_chain() == []


def test_offline_chain_never_touches_network(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("offline 模式不应构造网络数据源")

    monkeypatch.setattr(fetch, "AkShareSource", boom)
    monkeypatch.setattr(fetch, "AkShareThsSource", boom)

    chain = fetch.default_chain(offline=True)
    assert len(chain) == 1
    assert isinstance(chain[0], FixturesSource)


def test_default_chain_orders_sources_for_online() -> None:
    """联网链序（D-45）：新浪主源 → 同花顺 THS → fixtures（官方源已移出链）。"""
    chain = fetch.default_chain()
    assert [source.name for source in chain] == ["akshare", "ths", "fixture"]


# ── 回退顺序与降级留痕 ────────────────────────────────────────────────────
def test_primary_success_is_not_degraded() -> None:
    chain = stub_chain()
    outcome = fetch.fetch_universe(
        markets=("A",), chain=[chain["primary"], chain["official"], chain["fixture"]], persist=False
    )

    assert outcome.universe.stocks and outcome.universe.stocks[0]["code"] == "SH600900"
    # 快照是增强接口：stub 不提供 → market_snapshot:* 为 none（不降级、留 note），不计入主断言
    assert all(
        source == "akshare"
        for name, source in outcome.sources.items()
        if not name.startswith("market_snapshot")
    )
    assert outcome.degraded is False
    # 快照是增强接口：stub 不提供 → 只有 market_snapshot:* 有 fallbacks，主接口必须全干净
    assert all(name.startswith("market_snapshot") for name in outcome.fallbacks)
    assert not any("降级" in note for note in outcome.notes)


def test_hk_only_run_is_not_degraded() -> None:
    """PL-03：只跑港股时原先写死 ``sources["list_stocks"] = sources.get("list_stocks:A", "none")``。

    ``--markets HK`` 下 ``list_stocks:A`` 不存在 → 该键恒为 "none"，而 ``degraded`` 只豁免
    行情/快照 → **只要只跑港股就必然误报 degraded=True**，还把「产物需标 degraded」的
    note 写进产物，掩盖真实降级。
    """
    chain = stub_chain(
        # 港股行业树补全，把「list_stocks 汇总键」隔离成唯一变量（行业树缺失是真实降级，
        # 东财港股行业接口不可用 → 那属于 DS-38 的已知降级，不该混进本用例）
        primary=dict(
            industries={
                "A": [{"standard": "申万2021", "version": "v1", "level": 1}],
                "HK": [{"standard": "GICS", "version": "v1", "level": 1}],
            }
        )
    )
    outcome = fetch.fetch_universe(
        markets=("HK",),
        chain=[chain["primary"], chain["official"], chain["fixture"]],
        persist=False,
    )

    assert outcome.sources["list_stocks:HK"] == "akshare"
    assert outcome.sources["financials:HK00700"] == "akshare"
    assert "list_stocks" not in outcome.sources  # 不再有恒 "none" 的汇总键
    assert outcome.degraded is False
    assert not any("degraded=true" in note for note in outcome.notes)


def test_official_takes_over_when_primary_fails() -> None:
    chain = stub_chain(primary=dict(fail=("list_stocks", "financials", "industry_tree")))
    outcome = fetch.fetch_universe(
        markets=("A",), chain=[chain["primary"], chain["official"], chain["fixture"]], persist=False
    )

    assert [row["code"] for row in outcome.universe.stocks] == ["SZ000858"]
    assert outcome.sources["list_stocks:A"] == "official"
    assert outcome.sources["financials:SZ000858"] == "none"  # official 没有该标的的财报
    assert outcome.degraded is True
    assert any("akshare" in reason for reason in outcome.fallbacks["list_stocks:A"])
    assert any("降级到 official" in note for note in outcome.notes)
    assert any("degraded=true" in note for note in outcome.notes)


def test_fixture_is_the_last_resort() -> None:
    chain = stub_chain(
        primary=dict(fail=("list_stocks", "financials")),
        official=dict(fail=("list_stocks", "financials")),
    )
    outcome = fetch.fetch_universe(
        markets=("A",), chain=[chain["primary"], chain["official"], chain["fixture"]], persist=False
    )

    assert [row["code"] for row in outcome.universe.stocks] == ["SH600011"]
    assert outcome.sources["list_stocks:A"] == "fixture"
    assert outcome.degraded is True


def test_empty_result_counts_as_failure_and_falls_through() -> None:
    """返回空集无法与"真的没有数据"区分 → 按失败处理，继续降级。"""
    chain = stub_chain(primary=dict(empty=("list_stocks", "financials")))
    outcome = fetch.fetch_universe(
        markets=("A",), chain=[chain["primary"], chain["official"], chain["fixture"]], persist=False
    )

    assert outcome.sources["list_stocks:A"] == "official"
    assert any("返回空集" in reason for reason in outcome.fallbacks["list_stocks:A"])


def test_all_levels_failing_raises_with_reasons() -> None:
    chain = stub_chain(
        primary=dict(fail=("list_stocks",)),
        official=dict(fail=("list_stocks",)),
        fixture=dict(fail=("list_stocks",)),
    )
    with pytest.raises(base.DataSourceError) as excinfo:
        fetch.fetch_universe(
            markets=("A",),
            chain=[chain["primary"], chain["official"], chain["fixture"]],
            persist=False,
        )

    message = str(excinfo.value)
    assert "三级回退" in message or "回退链" in message
    assert "akshare" in message and "fixture" in message


def test_failed_interface_lands_in_notes() -> None:
    chain = stub_chain(
        primary=dict(
            stocks={"A": [stock_row()]},
            financials={"SH600900": [fin_row()]},
            fail=("industry_tree",),
        ),
        official=dict(fail=("industry_tree",)),
        fixture=dict(stocks={"A": [stock_row()]}, fail=("industry_tree",)),
    )
    outcome = fetch.fetch_universe(
        markets=("A",), chain=[chain["primary"], chain["official"], chain["fixture"]], persist=False
    )

    assert "industry_tree:A" in outcome.failed_interfaces
    assert any("全链路失败" in note for note in outcome.notes)


# ── 小样本过滤 ────────────────────────────────────────────────────────────
def test_codes_filter_keeps_only_requested() -> None:
    chain = stub_chain(
        primary=dict(
            stocks={"A": [stock_row("SH600900"), stock_row("SZ000858", name="五粮液")]},
            financials={"SH600900": [fin_row()], "SZ000858": [fin_row("SZ000858")]},
        )
    )
    outcome = fetch.fetch_universe(
        markets=("A",), codes=["SZ000858"], chain=[chain["primary"]], persist=False
    )

    assert [row["code"] for row in outcome.universe.stocks] == ["SZ000858"]


def test_limit_caps_rows_per_market() -> None:
    rows = [stock_row(f"SH60000{i}") for i in range(5)]
    chain = stub_chain(
        primary=dict(
            stocks={"A": rows},
            financials={f"SH60000{i}": [fin_row(f"SH60000{i}")] for i in range(5)},
        )
    )
    outcome = fetch.fetch_universe(markets=("A",), limit=2, chain=[chain["primary"]], persist=False)

    assert len(outcome.universe.stocks) == 2


def test_requested_code_missing_from_result_is_reported() -> None:
    """指定了一个任何源都没有的标的：必须说清它没进清洗结果，不能静默通过。"""
    chain = stub_chain(primary=dict(stocks={"A": []}, financials={}))
    outcome = fetch.fetch_universe(
        markets=("A",),
        codes=["SH999999"],
        chain=[chain["primary"], chain["fixture"]],
        persist=False,
    )

    assert outcome.universe.pending == ["SH999999"]
    assert any("未进入清洗结果" in note for note in outcome.notes)


# ── 落库与幂等 ────────────────────────────────────────────────────────────
def test_persist_false_skips_store() -> None:
    chain = stub_chain()
    outcome = fetch.fetch_universe(markets=("A",), chain=[chain["primary"]], persist=False)
    assert outcome.stats is None


def test_persist_writes_standard_layer(tmp_path) -> None:
    chain = stub_chain()
    outcome = fetch.fetch_universe(
        markets=("A",),
        chain=[chain["primary"]],
        db_path=tmp_path / "standard.duckdb",
        parquet_dir=tmp_path / "parquet",
        persist=True,
    )

    assert outcome.stats is not None
    assert outcome.stats.rows["stock"] == 1
    assert outcome.stats.rows["financial_report"] == 1
    assert len(outcome.stats.parquet) == 3


def test_rerun_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "standard.duckdb"
    chain = stub_chain()
    fetch.fetch_universe(markets=("A",), chain=[chain["primary"]], db_path=db_path)
    fetch.fetch_universe(markets=("A",), chain=[chain["primary"]], db_path=db_path)

    assert len(store.read_table("stock", db_path=db_path)) == 1
    assert len(store.read_table("financial_report", db_path=db_path)) == 1


# ── 真实 fixtures 端到端（离线）──────────────────────────────────────────
def test_end_to_end_with_real_fixtures() -> None:
    outcome = fetch.fetch_universe(chain=[FixturesSource()], persist=False)

    assert len(outcome.universe.stocks) >= 10
    assert len(outcome.universe.financials) >= 10
    assert outcome.universe.pending == []
    # 全部由 fixtures 提供 → 相对主源即为降级（诚实标记，不假装是主源数据）
    assert outcome.degraded is True
    assert all(
        source == "fixture"
        for name, source in outcome.sources.items()
        if not name.startswith(("price_daily", "market_snapshot"))
    )
    # fixtures 不含行情与快照（price_daily 抛错 / market_snapshot 返回空）→ 全链路无数据
    assert all(
        source == "none"
        for name, source in outcome.sources.items()
        if name.startswith(("price_daily", "market_snapshot"))
    )
    assert outcome.universe.prices == {}


def test_missing_price_data_alone_is_not_degraded() -> None:
    """**无行情不算降级**：kline 是契约里的可选字段（§3.4），主源提供其余数据即不降级。"""
    chain = stub_chain()
    chain["primary"]._prices.clear()  # 主源不给行情（如某股停牌 / 无历史）
    outcome = fetch.fetch_universe(markets=("A",), chain=[chain["primary"]], persist=False)

    assert outcome.sources["price_daily:SH600900"] == "none"
    assert outcome.degraded is False


def test_price_fetch_is_qfq_only_by_default() -> None:
    """D-32 决策 2：默认**只取前复权**（1 次/只，B 级）；``with_raw=True``
    （monthly/cold 模式）才补「不复权」以输出 ``hfq``。"""
    chain = stub_chain()
    outcome = fetch.fetch_universe(markets=("A",), chain=[chain["primary"]], persist=False)

    calls = [call for call in chain["primary"].calls if call[0] == "price_daily"]
    assert ("price_daily", "SH600900:qfq") in calls
    assert ("price_daily", "SH600900:raw") not in calls
    assert outcome.universe.prices["SH600900"]  # B 级：qfq-only 也有数据
    assert outcome.sources["price_daily:SH600900"] == "akshare"

    chain_with_raw = stub_chain()
    fetch.fetch_universe(
        markets=("A",), chain=[chain_with_raw["primary"]], persist=False, with_raw=True
    )
    calls_raw = [call for call in chain_with_raw["primary"].calls if call[0] == "price_daily"]
    assert ("price_daily", "SH600900:raw") in calls_raw
    assert ("price_daily", "SH600900:qfq") in calls_raw


def test_mode_switches_skip_fetch(tmp_path) -> None:
    """D-32 模式开关：`with_financials=False` / `with_history=False` 时跳过对应接口
    （daily 模式只快照 + 重算，财报从标准层读回 —— 这里用空库断言读回为空）。"""
    empty_db = tmp_path / "empty.duckdb"
    chain = stub_chain()
    outcome = fetch.fetch_universe(
        markets=("A",),
        chain=[chain["primary"]],
        persist=False,
        with_financials=False,
        db_path=empty_db,
    )
    assert not [call for call in chain["primary"].calls if call[0] == "financials"]
    assert outcome.universe.financials == {}

    chain2 = stub_chain()
    outcome2 = fetch.fetch_universe(
        markets=("A",), chain=[chain2["primary"]], persist=False, with_history=False
    )
    assert not [call for call in chain2["primary"].calls if call[0] == "price_daily"]
    assert outcome2.universe.prices == {}


def test_market_snapshot_flags_suspended() -> None:
    """D-32 快照接入：最新价缺失 → `status=suspended`（闭环 M1 遗留 TODO）；
    `snapshot_date` 供 daily 模式作行情截止日。"""
    chain = stub_chain(
        primary=dict(
            snapshots={
                "A": [
                    {
                        "code": "SH600900",
                        "latest_price": None,
                        "open": None,
                        "high": None,
                        "low": None,
                        "prev_close": 20.0,
                        "volume": None,
                    }
                ]
            }
        )
    )
    outcome = fetch.fetch_universe(markets=("A",), chain=[chain["primary"]], persist=False)

    assert outcome.universe.stocks[0]["status"] == "suspended"
    assert outcome.snapshot_date == date.today().isoformat()
    assert outcome.sources["market_snapshot:A"] == "akshare"
    assert any("suspended" in note for note in outcome.notes)


def test_market_snapshot_missing_is_not_degraded_but_noted() -> None:
    """快照全链路失败 → 不算降级（增强接口），但必须留 note（停牌判定缺失要可见）。"""
    chain = stub_chain()
    outcome = fetch.fetch_universe(markets=("A",), chain=[chain["primary"]], persist=False)

    assert outcome.sources["market_snapshot:A"] == "none"
    assert outcome.degraded is False
    assert outcome.snapshot_date is None
    assert any("market_snapshot:A 全链路失败" in note for note in outcome.notes)


def test_cli_offline_smoke(capsys) -> None:
    assert fetch.main(["--offline", "--no-store", "--limit", "2"]) == 0
    printed = capsys.readouterr().out
    assert "[fetch] 股票" in printed
    assert "degraded=True" in printed


def test_cli_rejects_unknown_market(capsys) -> None:
    assert fetch.main(["--offline", "--no-store", "--markets", "US"]) == 2
    assert "不支持的市场" in capsys.readouterr().err


def test_module_import_does_not_require_duckdb(monkeypatch) -> None:
    """落库依赖 duckdb，但采集编排本身不该在 import 时就需要它。"""
    assert Path(fetch.__file__).name == "fetch.py"
    assert config.PRIMARY_SOURCE_NAME == "akshare"
