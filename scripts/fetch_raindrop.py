#!/usr/bin/env python3
"""Fetch unprocessed X-share bookmarks from Raindrop.io.

Reads TEST_TOKEN and RAINDROP_COLLECTION_ID from .env at the repo root, pulls
items from that collection which lack the `processed` tag, writes one
Markdown file per item under knowledges/x-signals/, then adds the
`processed` tag on Raindrop so the item is not fetched again.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from config import env  # noqa: E402
OUTPUT_DIR = REPO_ROOT / "knowledges" / "x-signals"
PROCESSED_TAG = "processed"
API_BASE = "https://api.raindrop.io/rest/v1"
PAGE_SIZE = 50


def api_request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_unprocessed(token: str, collection_id: str) -> list[dict]:
    # Raindrop search grammar: `-#tag` excludes items with that tag.
    search = urllib.parse.quote(f"-#{PROCESSED_TAG}")
    items: list[dict] = []
    page = 0
    while True:
        result = api_request(
            "GET",
            f"/raindrops/{collection_id}?perpage={PAGE_SIZE}&page={page}&search={search}",
            token,
        )
        batch = result.get("items", [])
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return items


def output_path_for(item: dict) -> Path:
    created = item.get("created") or ""
    date_part = created[:10] if len(created) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return OUTPUT_DIR / f"{date_part}-{item['_id']}.md"


def render_markdown(item: dict) -> str:
    frontmatter = {
        "raindrop_id": item.get("_id"),
        "url": item.get("link"),
        "title": item.get("title"),
        "created": item.get("created"),
        "tags": item.get("tags") or [],
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {item.get('title') or '(no title)'}")
    lines.append("")
    lines.append(f"<{item.get('link') or ''}>")
    lines.append("")
    note = (item.get("note") or "").strip()
    if note:
        lines.extend(["## Note (from Raindrop)", "", note, ""])
    excerpt = (item.get("excerpt") or "").strip()
    if excerpt:
        lines.extend(["## Excerpt", "", excerpt, ""])
    return "\n".join(lines)


def mark_processed(item: dict, token: str) -> None:
    existing = item.get("tags") or []
    new_tags = sorted({*existing, PROCESSED_TAG})
    api_request("PUT", f"/raindrop/{item['_id']}", token, {"tags": new_tags})


def main() -> int:
    token = env("TEST_TOKEN")
    collection_id = env("RAINDROP_COLLECTION_ID")
    if not collection_id:
        print("error: RAINDROP_COLLECTION_ID not set in .env", file=sys.stderr)
        return 1
    if not token:
        print("error: TEST_TOKEN not set in .env", file=sys.stderr)
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        items = fetch_unprocessed(token, collection_id)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"error: fetch failed: HTTP {exc.code} {body}", file=sys.stderr)
        return 1

    if not items:
        print("no unprocessed items")
        return 0

    written = 0
    for item in items:
        out_path = output_path_for(item)
        out_path.write_text(render_markdown(item), encoding="utf-8")
        try:
            mark_processed(item, token)
        except urllib.error.HTTPError as exc:
            print(f"warn: failed to tag id={item['_id']}: HTTP {exc.code}", file=sys.stderr)
            continue
        written += 1
        print(f"wrote {out_path.relative_to(REPO_ROOT)}")
    print(f"done: {written}/{len(items)} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
