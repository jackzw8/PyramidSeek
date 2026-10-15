"""前端静态资源检查（M4）：页面引用的资源必须存在，且不残留演示数据依赖。

纯文件检查（不需要浏览器 / 服务器），可在 CI 跑；DOM 级冒烟见 `temp/npmtest/smoke_dom.js`
（jsdom，不入库）。
"""

from __future__ import annotations

import re
from pathlib import Path

from src import config

PUBLIC = config.PUBLIC_DIR
PAGES = ("index.html", "stock.html", "watchlist.html")
# 原型演示期的依赖：正式版数据一律来自产物（api.js），不得回退
BANNED = ("js/data.js", "js/engine.js", "PS_DATA", "PS.all()", "PS.rng")


def _pages() -> list[Path]:
    return [PUBLIC / page for page in PAGES]


def test_pages_exist() -> None:
    for path in _pages():
        assert path.exists(), f"缺少页面：{path}"


def test_referenced_assets_exist() -> None:
    for path in _pages():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'(?:src|href)="([^"#?]+)"', text):
            asset = match.group(1)
            if asset.startswith(("http", "mailto")):
                continue
            assert (PUBLIC / asset).exists(), f"{path.name} 引用了不存在的资源：{asset}"


def test_pages_load_shared_modules() -> None:
    for path in _pages():
        text = path.read_text(encoding="utf-8")
        for module in ("js/api.js", "js/common.js", "js/search.js"):
            assert module in text, f"{path.name} 未引入 {module}"


def test_no_demo_data_dependency() -> None:
    targets = [*_pages(), *(PUBLIC / "js").glob("*.js")]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for banned in BANNED:
            assert banned not in text, f"{path.name} 残留演示数据依赖：{banned}"


def test_js_files_exist() -> None:
    expected = {"api.js", "common.js", "ranking.js", "search.js", "stock.js", "watchlist.js"}
    actual = {path.name for path in (PUBLIC / "js").glob("*.js")}
    assert expected <= actual, f"缺少前端脚本：{sorted(expected - actual)}"


def test_detail_grid_keeps_three_columns_wide() -> None:
    """详情页三列（评分构成｜雷达｜同业）在 ≥960px 同排；断点不得回退到过保守的值。"""
    css = (PUBLIC / "css" / "style.css").read_text(encoding="utf-8")
    assert ".grid3{display:grid;grid-template-columns:1.1fr 1fr 232px" in css, "三列列宽声明被改动"
    assert "@media(max-width:960px)" in css, "三列断点应为 960px"


def test_method_card_carries_config_values() -> None:
    """排行页「评分说明」卡的权重与锚点是静态文案 —— 断言与 config 一致，防公式漂移。"""
    text = (PUBLIC / "index.html").read_text(encoding="utf-8")
    for weight in config.WEIGHTS.values():
        assert f"· {weight}" in text, f"评分说明卡缺权重 {weight}"
    for key in ("roe", "gross_margin", "cash_ratio"):  # 卡片只列了这三项锚点
        segments = "、".join(
            f"{value * 100:g}%→{score:g}" for value, score in config.ABS_ANCHORS[key] if value > 0
        )
        assert segments in text, f"评分说明卡锚点与 config 不一致：{key}（应为 {segments}）"
