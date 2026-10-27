"""pytest 全局配置：定位「演示版前端」（双引擎对账的 JS 参考实现，家法 R13）。

**为什么需要单独定位**：`pyramidseek-web/js/engine.js` 是文档工作区里的原型资产，
不在本仓库内。2026-10-27 目录搬迁后，代码仓（``D:\\AIwork\\project\\PyramidSeekApp``）
与文档工作区（移动云盘挂载点）**不再同级**，原先硬编码的
``REPO.parent / "项目文档" / "pyramidseek-web" / "js"`` 直接失效 ——
5 个对账用例全部静默 skip，而 ``addopts = "-q"`` 又把 skip 原因吞掉，
``pytest`` 照样 exit 0，看起来「全绿」（审核项 TS-02 的恶化形态）。

定位顺序（先命中先用）：

1. 环境变量 ``PYRAMIDSEEK_WEB_JS``（js 目录，或直接给 ``engine.js`` 的路径）
2. ``pyproject.toml`` 的 ``demo_engine_js``（本机路径 —— **搬迁后只改这一处**）
3. 搬迁前的旧布局：代码仓同级下的 ``项目文档/pyramidseek-web/js``

``PYRAMIDSEEK_REQUIRE_ENGINE=1`` 时找不到直接 **fail** 而不是 skip，
用于把「对账必须真跑」变成门禁（本地或 CI）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent  # PyramidSeekApp/
_JS_REL = ("pyramidseek-web", "js")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "demo_engine_js",
        "演示版前端（pyramidseek-web/js）目录或 engine.js 的路径 —— 双引擎对账的参考实现",
        default="",
    )


def _resolve(raw: str | Path) -> Path | None:
    """把「目录 / engine.js 路径」统一成 js 目录；不成立返回 ``None``。"""
    path = Path(raw).expanduser()
    if path.name == "engine.js":
        path = path.parent
    return path if (path / "engine.js").is_file() else None


def demo_web_js(config: pytest.Config) -> Path | None:
    """定位演示版前端 js 目录；找不到返回 ``None``。"""
    candidates: list[str] = []
    env = os.environ.get("PYRAMIDSEEK_WEB_JS")
    if env:
        candidates.append(env)
    ini = config.getini("demo_engine_js")
    if ini:
        candidates.append(ini)
    # 搬迁前的旧布局：代码仓与 `项目文档/` 同级（保留，兼容未搬迁的环境）
    candidates.append(str(REPO.parent.joinpath("项目文档", *_JS_REL)))

    for raw in candidates:
        found = _resolve(raw)
        if found is not None:
            return found
    return None


@pytest.fixture(scope="session")
def demo_web_js_dir(request: pytest.FixtureRequest) -> Path:
    """演示版前端的 js 目录（含 ``engine.js`` / ``data.js``）；缺了按需 skip 或 fail。"""
    found = demo_web_js(request.config)
    if found is not None:
        return found
    hint = "设 PYRAMIDSEEK_WEB_JS 环境变量，或 pyproject.toml 的 demo_engine_js"
    if os.environ.get("PYRAMIDSEEK_REQUIRE_ENGINE") == "1":
        pytest.fail(f"找不到演示版前端（pyramidseek-web/js/engine.js）：{hint}")
    pytest.skip(f"演示前端不在本机已知位置：{hint}")
