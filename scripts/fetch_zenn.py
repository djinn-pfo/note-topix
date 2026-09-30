#!/usr/bin/env python3
"""Fetch Zenn trending articles via Zenn's public (unofficial) JSON API.

Endpoint is what zenn.dev itself calls from the browser; stable in practice
but not contractually documented. Falls back to logging if the shape shifts.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "knowledges" / "zenn-signals"
API_URL = "https://zenn.dev/api/articles?order=daily"


def fetch_articles() -> list[dict]:
    result = subprocess.run(
        ["curl", "-sS", "--fail-with-body", "-A", "note-topix/0.1", API_URL],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exit {result.returncode}: {result.stderr.strip() or result.stdout[:200]}")
    payload = json.loads(result.stdout)
    return payload.get("articles", [])


def output_path_for(art: dict) -> Path:
    date_part = (art.get("published_at") or "")[:10] or "unknown-date"
    return OUTPUT_DIR / f"{date_part}-{art['id']}.md"


def render_markdown(art: dict) -> str:
    url = f"https://zenn.dev{art.get('path', '')}"
    user = art.get("user") or {}
    frontmatter = {
        "source": "zenn",
        "zenn_id": art.get("id"),
        "slug": art.get("slug"),
        "url": url,
        "title": art.get("title"),
        "emoji": art.get("emoji"),
        "article_type": art.get("article_type"),
        "author_username": user.get("username"),
        "author_name": user.get("name"),
        "published_at": art.get("published_at"),
        "liked_count": art.get("liked_count"),
        "bookmarked_count": art.get("bookmarked_count"),
        "comments_count": art.get("comments_count"),
        "body_letters_count": art.get("body_letters_count"),
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {art.get('emoji', '')} {art.get('title') or '(no title)'}")
    lines.append("")
    lines.append(f"- URL: <{url}>")
    lines.append(f"- Author: {user.get('name')} (@{user.get('username')})")
    lines.append(
        f"- Likes: {art.get('liked_count')} · Bookmarks: {art.get('bookmarked_count')} · Comments: {art.get('comments_count')}"
    )
    lines.append(f"- Length: {art.get('body_letters_count')} chars")
    return "\n".join(lines) + "\n"


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        articles = fetch_articles()
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    seen_new = 0
    seen_dup = 0
    for art in articles:
        out = output_path_for(art)
        if out.exists():
            seen_dup += 1
            continue
        out.write_text(render_markdown(art), encoding="utf-8")
        seen_new += 1
    print(f"done: {seen_new} new, {seen_dup} duplicates skipped (order=daily, {len(articles)} candidates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
