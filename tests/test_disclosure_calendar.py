"""披露日历与财报 TTL 分档（D-52）的边界测试。

锁三件事：**北京日期换算**（不能用 UTC 日期判窗口）、**窗口端点**（含端点/缓冲）、
**应披露报告期的切换点**（预筛判定的唯一依据）。全程纯函数，不碰网络与库。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from src import common, config


# ── 北京日期（披露窗口的判定基准）────────────────────────────────────────
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 11, 3, 15, 0, tzinfo=UTC), date(2026, 11, 3)),  # 北京 23:00 同日
        (datetime(2026, 11, 3, 17, 0, tzinfo=UTC), date(2026, 11, 4)),  # 北京次日 01:00
        (datetime(2026, 12, 31, 16, 30, tzinfo=UTC), date(2027, 1, 1)),  # 跨年
    ],
)
def test_beijing_today_crosses_midnight(moment: datetime, expected: date) -> None:
    assert common.beijing_today(moment) == expected


def test_beijing_today_defaults_to_now() -> None:
    assert isinstance(common.beijing_today(), date)


# ── 披露窗口（含端点；端点日仍算窗口内）──────────────────────────────────
@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 1, 1), "年报/一季报"),
        (date(2026, 4, 30), "年报/一季报"),
        (date(2026, 5, 5), "年报/一季报"),  # 缓冲末日内
        (date(2026, 5, 6), None),  # 淡季起点
        (date(2026, 6, 30), None),
        (date(2026, 7, 1), "半年报"),
        (date(2026, 9, 5), "半年报"),
        (date(2026, 9, 6), None),
        (date(2026, 10, 1), "三季报"),
        (date(2026, 11, 5), "三季报"),
        (date(2026, 11, 6), None),
        (date(2026, 12, 31), None),
    ],
)
def test_disclosure_window(today: date, expected: str | None) -> None:
    assert common.disclosure_window(today) == expected


# ── TTL 分档 ────────────────────────────────────────────────────────────
def test_financials_ttl_days_by_season() -> None:
    assert common.financials_ttl_days(date(2026, 4, 20)) == config.FINANCIALS_FRESH_DAYS
    assert common.financials_ttl_days(date(2026, 8, 20)) == config.FINANCIALS_FRESH_DAYS
    assert common.financials_ttl_days(date(2026, 11, 5)) == config.FINANCIALS_FRESH_DAYS
    assert common.financials_ttl_days(date(2026, 6, 15)) == config.FINANCIALS_FRESH_DAYS_OFF_SEASON
    assert common.financials_ttl_days(date(2026, 12, 1)) == config.FINANCIALS_FRESH_DAYS_OFF_SEASON
    assert config.FINANCIALS_FRESH_DAYS_OFF_SEASON > config.FINANCIALS_FRESH_DAYS


# ── 应披露报告期（预筛依据）──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 1, 1), "2025-09-30"),  # 年报季尚未启动
        (date(2026, 1, 24), "2025-09-30"),
        (date(2026, 1, 25), "2025-12-31"),  # 年报季启动
        (date(2026, 3, 31), "2025-12-31"),
        (date(2026, 4, 1), "2026-03-31"),  # 一季报季启动（先于 4/30 截止）
        (date(2026, 5, 4), "2026-03-31"),
        (date(2026, 6, 30), "2026-03-31"),
        (date(2026, 7, 1), "2026-06-30"),  # 半年报季启动
        (date(2026, 9, 4), "2026-06-30"),
        (date(2026, 10, 1), "2026-09-30"),  # 三季报季启动（关键：10 月整月都要 Q3）
        (date(2026, 11, 3), "2026-09-30"),
        (date(2026, 12, 31), "2026-09-30"),
    ],
)
def test_expected_report_period(today: date, expected: str) -> None:
    assert common.expected_report_period(today) == expected


def test_expected_report_period_is_monotonic_within_year() -> None:
    """年内应披露期只会向后推进，不能回退（回退会导致重复重抓）。"""
    checkpoints = [
        date(2026, 1, 1),
        date(2026, 2, 1),
        date(2026, 4, 1),
        date(2026, 7, 1),
        date(2026, 10, 1),
        date(2026, 12, 31),
    ]
    periods = [common.expected_report_period(day) for day in checkpoints]
    assert periods == sorted(periods)
