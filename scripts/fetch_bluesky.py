#!/usr/bin/env python3
"""Fetch recent posts from Bluesky's "Japan Hub" feed generator.

No auth needed for `app.bsky.feed.getFeed` against public.api.bsky.app.
The feed URI is community-run — if it disappears, swap FEED_URI for another
one from `app.bsky.unspecced.getPopularFeedGenerators?query=japan`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "knowledges" / "bluesky-signals"
API_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getFeed"
FEED_URI = "at://did:plc:vzwsee2cdqdehm3xldd3w4zn/app.bsky.feed.generator/aaaeju55uglx2"  # Japan Hub
PAGE_LIMIT = 100
MAX_PAGES = 3  # up to 300 posts


def fetch_page(cursor: str | None) -> dict:
    params: dict[str, str | int] = {"feed": FEED_URI, "limit": PAGE_LIMIT}
    if cursor:
        params["cursor"] = cursor
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    result = subprocess.run(
        ["curl", "-sS", "--fail-with-body", "-A", "note-topix/0.1", url],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exit {result.returncode}: {result.stderr.strip() or result.stdout[:200]}")
    return json.loads(result.stdout)


def post_id_from_uri(uri: str) -> str:
    # at://did:plc:xxx/app.bsky.feed.post/RKEY  -> did-plc-xxx--RKEY
    match = re.match(r"at://(did:[^/]+)/app\.bsky\.feed\.post/([^/]+)", uri)
    if not match:
        return re.sub(r"[^A-Za-z0-9._-]", "_", uri)
    did = match.group(1).replace(":", "-")
    return f"{did}--{match.group(2)}"


def bsky_web_url(uri: str, handle: str | None) -> str:
    # https://bsky.app/profile/<handle>/post/<rkey>
    match = re.match(r"at://(did:[^/]+)/app\.bsky\.feed\.post/([^/]+)", uri)
    if not match or not handle:
        return uri
    return f"https://bsky.app/profile/{handle}/post/{match.group(2)}"


def output_path_for(post: dict) -> Path:
    record = post.get("record") or {}
    created_at = record.get("createdAt") or ""
    date_part = created_at[:10] if len(created_at) >= 10 else "unknown-date"
    return OUTPUT_DIR / f"{date_part}-{post_id_from_uri(post['uri'])}.md"


def render_markdown(post: dict) -> str:
    author = post.get("author") or {}
    record = post.get("record") or {}
    handle = author.get("handle")
    frontmatter = {
        "source": "bluesky",
        "uri": post.get("uri"),
        "url": bsky_web_url(post.get("uri", ""), handle),
        "author_handle": handle,
        "author_display_name": author.get("displayName"),
        "author_did": author.get("did"),
        "created_at": record.get("createdAt"),
        "langs": record.get("langs") or [],
        "like_count": post.get("likeCount"),
        "repost_count": post.get("repostCount"),
        "reply_count": post.get("replyCount"),
        "quote_count": post.get("quoteCount"),
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    text = (record.get("text") or "").strip()
    lines.append(text)
    lines.append("")
    lines.append(f"- Post: <{bsky_web_url(post.get('uri', ''), handle)}>")
    lines.append(f"- Author: {author.get('displayName') or handle} (@{handle})")
    lines.append(
        f"- Likes: {post.get('likeCount')} · Reposts: {post.get('repostCount')} · Replies: {post.get('replyCount')}"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    seen_new = 0
    seen_dup = 0
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        try:
            payload = fetch_page(cursor)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        entries = payload.get("feed") or []
        for entry in entries:
            post = entry.get("post")
            if not post:
                continue
            out = output_path_for(post)
            if out.exists():
                seen_dup += 1
                continue
            out.write_text(render_markdown(post), encoding="utf-8")
            seen_new += 1
        cursor = payload.get("cursor")
        if not cursor or not entries:
            break
    print(f"done: {seen_new} new, {seen_dup} duplicates skipped (feed: Japan Hub)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
