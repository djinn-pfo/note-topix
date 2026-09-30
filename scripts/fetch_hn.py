#!/usr/bin/env python3
"""Fetch recent high-point Hacker News stories via the Algolia HN Search API.

No auth needed. Writes normalized markdown to knowledges/hn-signals/.
Dedup is by file existence: re-runs skip stories already on disk.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "knowledges" / "hn-signals"
API_URL = "https://hn.algolia.com/api/v1/search_by_date"
LOOKBACK_DAYS = 7
MIN_POINTS = 100
HITS_PER_PAGE = 50
MAX_PAGES = 4  # up to 200 stories per run


def fetch_page(page: int, since_ts: int) -> dict:
    params = {
        "tags": "story",
        "numericFilters": f"points>={MIN_POINTS},created_at_i>{since_ts}",
        "hitsPerPage": HITS_PER_PAGE,
        "page": page,
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "note-topix/0.1"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def output_path_for(hit: dict) -> Path:
    created = hit.get("created_at") or ""
    date_part = created[:10] if len(created) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return OUTPUT_DIR / f"{date_part}-{hit['objectID']}.md"


def render_markdown(hit: dict) -> str:
    story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
    frontmatter = {
        "source": "hackernews",
        "hn_id": hit["objectID"],
        "url": story_url,
        "hn_url": f"https://news.ycombinator.com/item?id={hit['objectID']}",
        "title": hit.get("title"),
        "author": hit.get("author"),
        "created": hit.get("created_at"),
        "points": hit.get("points"),
        "num_comments": hit.get("num_comments"),
        "tags": hit.get("_tags") or [],
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {hit.get('title') or '(no title)'}")
    lines.append("")
    lines.append(f"- Link: <{story_url}>")
    lines.append(f"- HN thread: <https://news.ycombinator.com/item?id={hit['objectID']}>")
    lines.append(f"- Points: {hit.get('points')} · Comments: {hit.get('num_comments')}")
    story_text = (hit.get("story_text") or "").strip()
    if story_text:
        lines.extend(["", "## Story text", "", story_text])
    return "\n".join(lines) + "\n"


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    since_ts = int(time.time()) - LOOKBACK_DAYS * 86400

    seen_new = 0
    seen_dup = 0
    for page in range(MAX_PAGES):
        try:
            payload = fetch_page(page, since_ts)
        except urllib.error.HTTPError as exc:
            print(f"error: HTTP {exc.code} on page {page}", file=sys.stderr)
            return 1
        hits = payload.get("hits", [])
        if not hits:
            break
        for hit in hits:
            out = output_path_for(hit)
            if out.exists():
                seen_dup += 1
                continue
            out.write_text(render_markdown(hit), encoding="utf-8")
            seen_new += 1
        if len(hits) < HITS_PER_PAGE:
            break

    print(f"done: {seen_new} new, {seen_dup} duplicates skipped (lookback {LOOKBACK_DAYS}d, min {MIN_POINTS} pts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
