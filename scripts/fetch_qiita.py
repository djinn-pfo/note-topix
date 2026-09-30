#!/usr/bin/env python3
"""Fetch recent high-stock Qiita items via the public v2 API.

Anonymous rate limit is 60 req/hour; we make at most a handful of calls per
run. Filters by stocks>=STOCKS_MIN and created within the last LOOKBACK_DAYS.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "knowledges" / "qiita-signals"
API_BASE = "https://qiita.com/api/v2/items"
LOOKBACK_DAYS = 7
STOCKS_MIN = 10
PER_PAGE = 100
MAX_PAGES = 2


def fetch_page(page: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    query = f"stocks:>{STOCKS_MIN} created:>{since}"
    params = {"per_page": PER_PAGE, "page": page, "query": query}
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    result = subprocess.run(
        ["curl", "-sS", "--fail-with-body", "-A", "note-topix/0.1", url],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exit {result.returncode}: {result.stderr.strip() or result.stdout[:200]}")
    return json.loads(result.stdout)


def output_path_for(item: dict) -> Path:
    date_part = (item.get("created_at") or "")[:10] or "unknown-date"
    return OUTPUT_DIR / f"{date_part}-{item['id']}.md"


def render_markdown(item: dict) -> str:
    user = item.get("user") or {}
    tags = [t.get("name") for t in (item.get("tags") or [])]
    frontmatter = {
        "source": "qiita",
        "qiita_id": item.get("id"),
        "url": item.get("url"),
        "title": item.get("title"),
        "author_id": user.get("id"),
        "author_name": user.get("name"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "likes_count": item.get("likes_count"),
        "stocks_count": item.get("stocks_count"),
        "comments_count": item.get("comments_count"),
        "page_views_count": item.get("page_views_count"),
        "tags": tags,
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {item.get('title') or '(no title)'}")
    lines.append("")
    lines.append(f"- URL: <{item.get('url')}>")
    lines.append(f"- Author: {user.get('name') or user.get('id')}")
    lines.append(
        f"- Likes: {item.get('likes_count')} · Stocks: {item.get('stocks_count')} · Comments: {item.get('comments_count')}"
    )
    if tags:
        lines.append(f"- Tags: {', '.join(tags)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    seen_new = 0
    seen_dup = 0
    total = 0
    for page in range(1, MAX_PAGES + 1):
        try:
            items = fetch_page(page)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print(f"error on page {page}: {exc}", file=sys.stderr)
            return 1
        if not items:
            break
        total += len(items)
        for item in items:
            out = output_path_for(item)
            if out.exists():
                seen_dup += 1
                continue
            out.write_text(render_markdown(item), encoding="utf-8")
            seen_new += 1
        if len(items) < PER_PAGE:
            break
    print(
        f"done: {seen_new} new, {seen_dup} duplicates skipped "
        f"(lookback {LOOKBACK_DAYS}d, stocks>{STOCKS_MIN}, {total} candidates)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
