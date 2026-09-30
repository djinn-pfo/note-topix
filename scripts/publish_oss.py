#!/usr/bin/env python3
"""Sync the public (code-only) copy of this repo.

The working repo holds fetched article bodies, the term DB, briefings, research
notes and the author's own drafts — none of which are published. The existing
git history also contains drafts, so the public repo is a *separate* checkout
with its own history rather than a remote of this one.

Publication is an **allowlist**: only the paths in PUBLISH are copied. A new
directory is invisible to this script until it is added here, which is the
point — a denylist would leak whatever someone forgets to exclude.

    uv run scripts/publish_oss.py plan             # what would be published
    uv run scripts/publish_oss.py sync             # copy into the public checkout
    uv run scripts/publish_oss.py sync --push      # ... then commit and push
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT.parent / "note-topix-public"

# Files and directories that go public. Directories are copied recursively,
# minus EXCLUDE_NAMES and anything matching EXCLUDE_SUFFIXES.
PUBLISH = [
    "README.md",
    "LICENSE",
    ".gitignore",
    ".env.example",
    "pyproject.toml",
    "uv.lock",
    "api",
    "scripts",
    "webapp/src",
    "webapp/index.html",
    "webapp/package.json",
    "webapp/pnpm-lock.yaml",
    "webapp/pnpm-workspace.yaml",
    "webapp/tsconfig.json",
    "webapp/vite.config.ts",
    "webapp/tailwind.config.js",
    "webapp/postcss.config.js",
]

# Never copied, even inside an allowlisted directory.
EXCLUDE_NAMES = {"__pycache__", ".venv", "node_modules", "dist", ".DS_Store", ".env"}
EXCLUDE_SUFFIXES = {".pyc", ".db", ".log", ".tsbuildinfo"}

def forbidden_values() -> list[str]:
    """Strings that must not appear in published code, derived from the local
    machine rather than hardcoded — so the list cannot go stale, and this file
    does not itself contain the values it is looking for.

    Sources: every .env value (deployment-specific by definition, including the
    API token), the local account name (leaks through absolute paths), the git
    email, and the Tailscale address.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from config import dotenv

    values = [v for v in dotenv().values() if len(v) >= 6]
    values.append(Path.home().name)
    for cmd in (["git", "config", "user.email"], ["tailscale", "ip", "-4"]):
        try:
            got = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=REPO_ROOT)
            values += [line.strip() for line in got.stdout.splitlines() if len(line.strip()) >= 6]
        except (OSError, subprocess.SubprocessError):
            continue
    return sorted({v for v in values})


def _skip(path: Path) -> bool:
    return path.name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES


def collect() -> list[Path]:
    """Repo-relative paths that would be published."""
    out: list[Path] = []
    for entry in PUBLISH:
        source = REPO_ROOT / entry
        if not source.exists():
            print(f"warning: {entry} が見つかりません", file=sys.stderr)
            continue
        if source.is_file():
            out.append(Path(entry))
            continue
        for path in sorted(source.rglob("*")):
            if path.is_dir() or any(_skip(p) for p in path.relative_to(REPO_ROOT).parents):
                continue
            if _skip(path):
                continue
            out.append(path.relative_to(REPO_ROOT))
    return out


def scan_forbidden(paths: list[Path]) -> list[tuple[Path, int, str]]:
    needles = forbidden_values()
    hits: list[tuple[Path, int, str]] = []
    for rel in paths:
        path = REPO_ROOT / rel
        if path.suffix in {".lock", ".yaml"} or rel.name == "pnpm-lock.yaml":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for needle in needles:
                if needle in line:
                    masked = line.strip().replace(needle, f"<{needle[:2]}…{len(needle)}字>")
                    hits.append((rel, number, masked[:120]))
    return hits


def cmd_plan(args: argparse.Namespace) -> int:
    paths = collect()
    total = sum((REPO_ROOT / p).stat().st_size for p in paths)
    by_top: dict[str, int] = {}
    for p in paths:
        by_top[p.parts[0]] = by_top.get(p.parts[0], 0) + 1
    print(f"公開対象: {len(paths)} ファイル / {total / 1024:.0f} KB")
    for top, n in sorted(by_top.items(), key=lambda kv: -kv[1]):
        print(f"  {top:24} {n:4} ファイル")
    if args.verbose:
        for p in paths:
            print(f"    {p}")
    hits = scan_forbidden(paths)
    if hits:
        print(f"\n⚠️  公開対象に環境固有の値が {len(hits)} 件あります:")
        for rel, number, line in hits:
            print(f"  {rel}:{number}: {line}")
        return 1
    print("\n環境固有の値の混入なし")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser()
    paths = collect()
    hits = scan_forbidden(paths)
    if hits and not args.force:
        print(f"中止: 環境固有の値が {len(hits)} 件見つかりました（plan で確認してください）", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    published = {str(p) for p in paths}
    copied = changed = 0
    for rel in paths:
        src, dst = REPO_ROOT / rel, out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        is_new = not dst.exists()
        if is_new or not filecmp.cmp(src, dst, shallow=False):
            shutil.copy2(src, dst)
            changed += 1
        copied += 1

    # Remove files that are no longer published (renames, deletions).
    stale = []
    for path in sorted(out_dir.rglob("*")):
        if path.is_dir() or ".git" in path.parts:
            continue
        rel = str(path.relative_to(out_dir))
        if rel not in published:
            stale.append(rel)
            path.unlink()
    print(f"同期: {copied} ファイル（更新 {changed} / 削除 {len(stale)}）→ {out_dir}")
    for rel in stale:
        print(f"  削除 {rel}")

    if not args.push:
        print("push するには --push を付けてください")
        return 0
    if not (out_dir / ".git").exists():
        print(f"error: {out_dir} は git リポジトリではありません", file=sys.stderr)
        return 1
    subprocess.run(["git", "add", "-A"], cwd=out_dir, check=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=out_dir, capture_output=True, text=True)
    if not status.stdout.strip():
        print("変更なし")
        return 0
    message = args.message or f"sync from working repo ({datetime.now():%Y-%m-%d})"
    subprocess.run(["git", "commit", "-m", message], cwd=out_dir, check=True)
    subprocess.run(["git", "push"], cwd=out_dir, check=True)
    print("pushed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="list what would be published and scan for local values")
    p.add_argument("--verbose", "-v", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("sync", help="copy the allowlist into the public checkout")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--push", action="store_true")
    p.add_argument("--message")
    p.add_argument("--force", action="store_true", help="sync even if local values were found")
    p.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
