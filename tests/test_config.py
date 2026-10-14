"""配置常量自检（任务 M0-2 验收：常量与技术说明书 §4 一致）。

目的：把「文档里的数值」固化成断言——任何人改 `config.py` 的权重/锚点/参数，
都会在这里被拦住，必须先改文档并升 `formulaVersion`（家法 R3）。
"""

from __future__ import annotations

from src import config


def test_weights_match_spec_and_sum_to_1000() -> None:
    assert config.WEIGHTS == {
        "roe": 300,
        "net_profit_scale": 180,
        "roa": 160,
        "gross_margin": 130,
        "cash_ratio": 120,
        "asset_turnover": 110,
    }
    assert sum(config.WEIGHTS.values()) == 1000


def test_abs_anchors_match_spec() -> None:
    assert config.ABS_ANCHORS == {
        "roe": [(0.00, 0.0), (0.10, 50.0), (0.20, 80.0), (0.35, 100.0)],
        "gross_margin": [(0.00, 0.0), (0.20, 50.0), (0.30, 80.0), (0.40, 100.0)],
        "net_margin": [(0.00, 0.0), (0.10, 50.0), (0.20, 80.0), (0.30, 100.0)],
        "cash_ratio": [(0.00, 0.0), (0.10, 50.0), (0.25, 80.0), (0.35, 100.0)],
        "roa": [(0.00, 0.0), (0.05, 50.0), (0.10, 80.0), (0.15, 100.0)],
    }
    for anchors in config.ABS_ANCHORS.values():
        assert anchors[0][1] == config.SUB_SCORE_MIN
        assert anchors[-1][1] == config.SUB_SCORE_MAX
        assert [x for x, _ in anchors] == sorted(x for x, _ in anchors)


def test_percentile_indicators_are_the_industry_ranked_ones() -> None:
    assert set(config.PERCENTILE_INDICATORS) == {"net_profit_scale", "asset_turnover"}
    # 走分位的指标不进绝对值锚点表
    assert set(config.PERCENTILE_INDICATORS) & set(config.ABS_ANCHORS) == set()
    # 净利率只进雷达图、不进 Q（锚点表有它，权重表没有它）
    assert set(config.PERCENTILE_INDICATORS) | set(config.ABS_ANCHORS) == set(config.WEIGHTS) | {
        "net_margin"
    }
    assert "net_margin" not in config.WEIGHTS


def test_score_bounds_and_growth_params_match_spec() -> None:
    assert (config.Q_MIN, config.Q_MAX) == (0.0, 1000.0)
    assert (config.SCORE_MIN, config.SCORE_MAX) == (0.0, 1000.0)
    assert (config.GROWTH_MIN_TOTAL, config.GROWTH_MAX_TOTAL) == (-200.0, 200.0)
    assert config.GROWTH_MAX == 100.0
    assert config.CAGR_CAP == 0.20
    assert (config.CV_LOW, config.CV_HIGH) == (0.15, 0.75)
    assert (config.K_MIN, config.K_MAX) == (0.6, 1.0)
    assert config.STABILITY_DECIMALS == 4
    assert config.GROWTH_ITEM_DECIMALS == 2


def test_data_and_pool_conventions_match_spec() -> None:
    assert config.YEARS == 5
    assert config.EFFECTIVE_LAG_MONTHS == 4
    assert config.INDUSTRY_FALLBACK_MIN == 30
    assert config.COMPARE_LEVEL_DEFAULT == "L2"
    assert config.COMPARE_LEVEL_FALLBACK == "L1"
    assert config.TOP_PCT == 0.10
    assert config.MARKETS == ("A", "HK")
    assert config.INDUSTRY_STANDARD == {"A": "申万2021", "HK": "GICS"}


def test_versions_and_assets_exist() -> None:
    assert config.FORMULA_VERSION == "v1.0"
    assert config.CONTRACT_VERSION == "1.1"
    assert config.FIXTURES_FILE.exists(), "fixtures 示例数据必须就位（家法 R16：离线永远可跑）"
    assert config.DISCLAIMER  # 免责声明随行（家法 R20）
