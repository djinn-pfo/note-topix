#!/usr/bin/env python3
"""Fetch Google Trends daily trending searches (JP) via the public RSS feed.

Endpoint returns ~10-20 top searches with traffic bands and 2-3 related news
articles each. No auth, no dependency beyond curl + stdlib.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "knowledges" / "google-trends-signals"
RSS_URL = "https://trends.google.com/trending/rss?geo=JP"
HT_NS = "https://trends.google.com/trending/rss"


def fetch_rss() -> str:
    result = subprocess.run(
        ["curl", "-sS", "--fail-with-body", "-A", "note-topix/0.1", RSS_URL],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exit {result.returncode}: {result.stderr.strip() or result.stdout[:200]}")
    return result.stdout


def ht(tag: str) -> str:
    return f"{{{HT_NS}}}{tag}"


def parse_pub_date(raw: str) -> str:
    # Input: "Thu, 17 Sep 2026 02:00:00 -0700" -> "2026-09-17"
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError):
        return "unknown-date"


def slugify(text: str) -> str:
    # Google Trends items don't have stable IDs; use a hash of the title to dedupe.
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def parse_item(item: ET.Element) -> dict:
    title = (item.findtext("title") or "").strip()
    pub_date_raw = (item.findtext("pubDate") or "").strip()
    approx_traffic = (item.findtext(ht("approx_traffic")) or "").strip()
    news_items = []
    for ni in item.findall(ht("news_item")):
        news_items.append(
            {
                "title": (ni.findtext(ht("news_item_title")) or "").strip(),
                "url": (ni.findtext(ht("news_item_url")) or "").strip(),
                "source": (ni.findtext(ht("news_item_source")) or "").strip(),
                "snippet": (ni.findtext(ht("news_item_snippet")) or "").strip(),
            }
        )
    return {
        "title": title,
        "date": parse_pub_date(pub_date_raw),
        "pub_date_raw": pub_date_raw,
        "approx_traffic": approx_traffic,
        "news_items": news_items,
    }


def output_path_for(rec: dict) -> Path:
    return OUTPUT_DIR / f"{rec['date']}-{slugify(rec['title'])}.md"


def render_markdown(rec: dict) -> str:
    frontmatter = {
        "source": "google_trends",
        "geo": "JP",
        "title": rec["title"],
        "date": rec["date"],
        "approx_traffic": rec["approx_traffic"],
        "news_item_count": len(rec["news_items"]),
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {rec['title']}")
    lines.append("")
    lines.append(f"- Approx traffic: {rec['approx_traffic']}")
    lines.append(f"- Date: {rec['date']}")
    if rec["news_items"]:
        lines.append("")
        lines.append("## Related news")
        lines.append("")
        for ni in rec["news_items"]:
            lines.append(f"- [{ni['title']}]({ni['url']}) — {ni['source']}")
            if ni["snippet"]:
                lines.append(f"  > {ni['snippet']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        xml_text = fetch_rss()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    seen_new = 0
    seen_dup = 0
    for item in items:
        rec = parse_item(item)
        if not rec["title"]:
            continue
        out = output_path_for(rec)
        if out.exists():
            seen_dup += 1
            continue
        out.write_text(render_markdown(rec), encoding="utf-8")
        seen_new += 1
    print(f"done: {seen_new} new, {seen_dup} duplicates skipped ({len(items)} candidates from RSS)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
