#!/usr/bin/env python3
"""Fetch recent arXiv submissions in AI/ML-adjacent categories.

Uses the public arXiv API (Atom XML). No auth. Rate-limits itself to
one request every 3s per arXiv guidance.

Adjust CATEGORIES to match the user's writing area if it drifts away
from AI/ML.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "knowledges" / "arxiv-signals"
# arXiv's frontend returns 406 for Python stdlib HTTP/1.1 clients but 200 for
# curl (HTTP/2). Shell out to curl to avoid maintaining an HTTP/2 client.
API_URL = "https://export.arxiv.org/api/query"
CATEGORIES = ["cs.AI", "cs.CL", "cs.LG", "cs.CV", "stat.ML", "cs.CY"]
MAX_RESULTS_PER_PAGE = 100
MAX_PAGES = 3  # up to 300 recent papers per run
POLITE_DELAY_SECONDS = 3.0

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def build_query() -> str:
    return " OR ".join(f"cat:{c}" for c in CATEGORIES)


def fetch_page(start: int) -> str:
    params = {
        "search_query": build_query(),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": start,
        "max_results": MAX_RESULTS_PER_PAGE,
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    result = subprocess.run(
        ["curl", "-sS", "--fail-with-body", "-A", "note-topix/0.1", url],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exit {result.returncode} at start={start}: {result.stderr.strip() or result.stdout[:200]}")
    return result.stdout


def arxiv_id_from_url(entry_id: str) -> str:
    # entry_id is like http://arxiv.org/abs/2509.12345v1
    match = re.search(r"abs/([^v]+?)(v\d+)?$", entry_id)
    return match.group(1) if match else entry_id.rsplit("/", 1)[-1]


def parse_entry(entry: ET.Element) -> dict:
    def text(tag: str) -> str:
        el = entry.find(f"atom:{tag}", NS)
        return (el.text or "").strip() if el is not None else ""

    entry_id = text("id")
    aid = arxiv_id_from_url(entry_id)
    authors = [
        (a.findtext("atom:name", default="", namespaces=NS) or "").strip()
        for a in entry.findall("atom:author", NS)
    ]
    categories = [
        c.attrib.get("term", "") for c in entry.findall("atom:category", NS)
    ]
    primary_cat_el = entry.find("arxiv:primary_category", NS)
    primary_category = primary_cat_el.attrib.get("term") if primary_cat_el is not None else None
    pdf_link = ""
    for link in entry.findall("atom:link", NS):
        if link.attrib.get("title") == "pdf":
            pdf_link = link.attrib.get("href", "")
            break

    return {
        "arxiv_id": aid,
        "abs_url": f"https://arxiv.org/abs/{aid}",
        "pdf_url": pdf_link or f"https://arxiv.org/pdf/{aid}",
        "title": " ".join(text("title").split()),
        "summary": text("summary"),
        "authors": authors,
        "published": text("published"),
        "updated": text("updated"),
        "primary_category": primary_category,
        "categories": categories,
    }


def output_path_for(rec: dict) -> Path:
    date_part = (rec.get("published") or "")[:10]
    if len(date_part) != 10:
        date_part = "unknown-date"
    return OUTPUT_DIR / f"{date_part}-{rec['arxiv_id']}.md"


def render_markdown(rec: dict) -> str:
    frontmatter = {
        "source": "arxiv",
        "arxiv_id": rec["arxiv_id"],
        "url": rec["abs_url"],
        "pdf_url": rec["pdf_url"],
        "title": rec["title"],
        "authors": rec["authors"],
        "published": rec["published"],
        "updated": rec["updated"],
        "primary_category": rec["primary_category"],
        "categories": rec["categories"],
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {rec['title']}")
    lines.append("")
    lines.append(f"- Abs: <{rec['abs_url']}>")
    lines.append(f"- PDF: <{rec['pdf_url']}>")
    if rec["authors"]:
        lines.append(f"- Authors: {', '.join(rec['authors'])}")
    if rec["primary_category"]:
        lines.append(f"- Primary: {rec['primary_category']}")
    lines.extend(["", "## Abstract", "", rec["summary"]])
    return "\n".join(lines) + "\n"


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    seen_new = 0
    seen_dup = 0
    for page in range(MAX_PAGES):
        start = page * MAX_RESULTS_PER_PAGE
        try:
            xml_text = fetch_page(start)
        except (RuntimeError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        root = ET.fromstring(xml_text)
        entries = root.findall("atom:entry", NS)
        if not entries:
            break
        for entry in entries:
            rec = parse_entry(entry)
            out = output_path_for(rec)
            if out.exists():
                seen_dup += 1
                continue
            out.write_text(render_markdown(rec), encoding="utf-8")
            seen_new += 1
        if len(entries) < MAX_RESULTS_PER_PAGE:
            break
        time.sleep(POLITE_DELAY_SECONDS)

    print(f"done: {seen_new} new, {seen_dup} duplicates skipped (categories: {', '.join(CATEGORIES)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
