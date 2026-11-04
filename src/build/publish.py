"""发布器（M5-6 / M5-7 / ADR-014，2026-11-04）。

把本地跑批产物一次性发布到 GitHub Pages：**门禁 → 构建 `dist/` → 推 `gh-pages` + tag**。

    python -m src.build.publish --check                 # 只跑门禁（pytest + 产物 + manifest）
    python -m src.build.publish --build                 # 构建 dist/（复制 public + 补 publishedAt）
    python -m src.build.publish --preview [--port 8010] # 本地预览 dist/
    python -m src.build.publish --push [--skip-build]   # 门禁 → 构建 → 推送

门禁（M5-7）任一失败 → 打印差异摘要并退出 1，**不产生任何远端提交**。
推送（M5-6）：临时 worktree 挂 `gh-pages` → 清空 → 覆盖 `dist/` → 单提交 + `snap-*` tag；
回滚 = `git push origin snap-旧tag^{commit}:gh-pages --force-with-lease`（09 手册 §5）。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import common
from .manifest import stamp_published_at, verify_manifest
from .validate import validate_products

__all__ = ["gate_check", "build_dist", "push_gh_pages", "main"]

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DIR = REPO_ROOT / "public"
DIST_DIR = REPO_ROOT / "dist"
DATA_SUBDIR = "data"
GH_PAGES_BRANCH = "gh-pages"
PUBLISH_TMP = REPO_ROOT.parent / "_ps_publish_tmp"


def _run(cmd: list[str], *, cwd: Path = REPO_ROOT) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def _beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def gate_check(data_dir: Path) -> list[str]:
    """发布门禁（M5-7）：pytest → 产物校验 → manifest 校验，返回问题列表。"""
    problems: list[str] = []

    print("[publish][gate 1/3] pytest …")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:warnings"],
        cwd=REPO_ROOT,
        timeout=900,
        check=False,
    )
    if result.returncode != 0:
        problems.append(f"pytest 未全绿（exit {result.returncode}）—— 先修测试再发布")

    print("[publish][gate 2/3] 产物校验（schema + 引用完整性）…")
    problems += list(validate_products(data_dir))

    print("[publish][gate 3/3] manifest 校验（文件 / 哈希 / 孤儿 / 版本一致）…")
    problems += verify_manifest(data_dir)
    return problems


def build_dist() -> Path:
    """构建 `dist/`：`public/` 全量复制 + `manifest.publishedAt` 补戳。"""
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    shutil.copytree(PUBLIC_DIR, DIST_DIR)
    stamp = stamp_published_at(DIST_DIR / DATA_SUBDIR)
    print(f"[publish] dist/ 构建完成（publishedAt={stamp}）→ {DIST_DIR}")
    return DIST_DIR


def push_gh_pages(dist_dir: Path) -> str:
    """把 `dist/` 覆盖到 `gh-pages` 分支（单提交）并打 `snap-*` tag，返回 tag 名。"""
    stamp = _beijing_now().strftime("%Y%m%d-%H%M%S")
    tag = f"snap-{stamp}"

    _run(["git", "worktree", "add", "--detach", str(PUBLISH_TMP)])
    try:
        _run(["git", "checkout", GH_PAGES_BRANCH], cwd=PUBLISH_TMP)
        for child in PUBLISH_TMP.iterdir():  # 清空旧快照（保留 .git）
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in dist_dir.iterdir():  # 覆盖新快照
            target = PUBLISH_TMP / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
        _run(["git", "add", "-A"], cwd=PUBLISH_TMP)
        _run(
            [
                "git",
                "commit",
                "-m",
                f"publish: {tag}",
                "-m",
                f"snapshotVersion={common.beijing_today().isoformat()}"
                "｜source: 见 data/meta.json sourcePolicy",
            ],
            cwd=PUBLISH_TMP,
        )
        _run(["git", "tag", tag], cwd=PUBLISH_TMP)
        _run(["git", "push", "origin", GH_PAGES_BRANCH])
        _run(["git", "push", "origin", tag])
    finally:
        _run(["git", "worktree", "remove", str(PUBLISH_TMP), "--force"])
    print(
        f"[publish] 已推送 {GH_PAGES_BRANCH}（tag {tag}）→ https://jackzw8.github.io/PyramidSeek/"
    )
    return tag


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布器（M5-6 / M5-7）：门禁 → dist → gh-pages")
    parser.add_argument("--check", action="store_true", help="只跑发布门禁，不构建不推送")
    parser.add_argument("--build", action="store_true", help="构建 dist/（不推送）")
    parser.add_argument("--push", action="store_true", help="门禁 → 构建 → 推送 gh-pages + tag")
    parser.add_argument(
        "--skip-build", action="store_true", help="推送时跳过构建（复用现成 dist/）"
    )
    parser.add_argument("--yes", action="store_true", help="跳过推送前的确认")
    args = parser.parse_args(argv)

    if not (args.check or args.build or args.push):
        parser.error("至少指定 --check / --build / --push 之一")

    data_dir = PUBLIC_DIR / DATA_SUBDIR
    if args.check or args.push:
        problems = gate_check(data_dir)
        if problems:
            print(f"[publish][BLOCKED] 门禁未通过（{len(problems)} 项）：", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print("[publish] 门禁通过")

    if args.push:
        if not args.skip_build:
            build_dist()
        problems = verify_manifest(DIST_DIR / DATA_SUBDIR)
        if problems:
            print("[publish][BLOCKED] dist/ 自校验失败：", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        if not args.yes:
            answer = input(f"确认推送 {DIST_DIR} → gh-pages？[y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("[publish] 已取消（未推送）")
                return 1
        push_gh_pages(DIST_DIR)
        return 0

    if args.build:
        build_dist()
    return 0


if __name__ == "__main__":
    sys.exit(main())
