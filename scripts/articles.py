#!/usr/bin/env python3
"""Published-article tracker for the 100-day posting challenge.

Registers note.com articles by URL, then re-reads their スキ / comment counts
from note's public API on every pipeline run, so the growth curve is kept
(`article_stats`) rather than just the latest number. `kind` records the
article format, which is what makes "did 「なぜ」 pieces actually land?"
answerable later with own data instead of a sampled guess.

    uv run scripts/articles.py add <note url> [--kind why] [--topic "..."] \\
        [--briefing documents/briefings/<f>.md] [--annex <slug>]
    uv run scripts/articles.py sync          # refresh stats for every article
    uv run scripts/articles.py list
    uv run scripts/articles.py streak        # 100-day challenge progress
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from config import env  # noqa: E402
from term_db import (  # noqa: E402
    connect,
    get_meta,
    set_meta,
    list_articles,
    record_article_stats,
    set_backlog_state,
    upsert_article,
)

NOTE_API = "https://note.com/api/v3/notes/{key}"
CREATOR_API = "https://note.com/api/v2/creators/{user}/contents?kind=note&page={page}"
# Own note.com account. Set NOTE_USER in .env or the environment (or pass --user).
DEFAULT_USER = env("NOTE_USER")
NOTE_KEY_RE = re.compile(r"/n/(n[0-9a-z]+)")
# Article formats. Mixing heavy and light ones is what makes a daily streak
# survivable: 「なぜ」 needs research, the others mostly need attention.
KINDS = ("why", "news", "retro", "glossary", "build", "other")
CHALLENGE_LENGTH = 100


def note_key_from_url(url: str) -> str:
    m = NOTE_KEY_RE.search(url)
    if not m:
        raise SystemExit(f"note の記事URLが読み取れません: {url}")
    return m.group(1)


def fetch_note(key: str) -> dict:
    req = urllib.request.Request(
        NOTE_API.format(key=key), headers={"User-Agent": "note-topix/0.1 (personal article tracker)"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["data"]


def fetch_creator_page(user: str, page: int) -> dict:
    req = urllib.request.Request(
        CREATOR_API.format(user=user, page=page),
        headers={"User-Agent": "note-topix/0.1 (personal article tracker)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["data"]


def cmd_import(args: argparse.Namespace) -> int:
    """Pull every published note of a creator — no need to register URLs by hand."""
    if not args.user:
        print("error: note のユーザー名が未設定です (.env の NOTE_USER か --user)", file=sys.stderr)
        return 1
    conn = connect()
    page, seen, new = 1, 0, 0
    existing = {a["note_key"] for a in list_articles(conn)}
    while page <= args.max_pages:
        data = fetch_creator_page(args.user, page)
        for n in data.get("contents", []):
            key = n.get("key")
            if not key:
                continue
            seen += 1
            is_new = key not in existing
            article_id = upsert_article(conn, {
                "note_key": key,
                "url": f"https://note.com/{args.user}/n/{key}",
                "title": n.get("name") or key,
                "published_on": (n.get("publishAt") or "")[:10],
                "price": n.get("price") or 0,
            })
            record_article_stats(conn, article_id, n.get("likeCount") or 0, n.get("commentCount") or 0)
            if is_new:
                new += 1
                print(f"  + {(n.get('publishAt') or '')[:10]}  スキ {n.get('likeCount') or 0:3}  {(n.get('name') or '')[:46]}")
        if data.get("isLastPage"):
            break
        page += 1
        time.sleep(args.delay)
    print(f"imported: {new} new / {seen} seen")
    print("kind (記事の型) は articles.py set か画面から後で付けられます")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    conn = connect()
    key = note_key_from_url(args.url) if "/" in args.url else args.url
    fields = {"note_key": key}
    for field in ("kind", "topic", "briefing", "annex", "memo"):
        value = getattr(args, field, None)
        if value is not None:
            fields["annex_slug" if field == "annex" else field] = value
    upsert_article(conn, fields)
    if args.briefing:
        row = [a for a in list_articles(conn) if a["note_key"] == key]
        set_backlog_state(conn, args.briefing, "published", article_id=row[0]["article_id"] if row else None)
    print(f"updated {key}: " + ", ".join(f"{k}={v}" for k, v in fields.items() if k != "note_key"))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    conn = connect()
    key = note_key_from_url(args.url)
    data = fetch_note(key)
    published = (data.get("publish_at") or "")[:10]
    article_id = upsert_article(conn, {
        "note_key": key,
        "url": args.url.split("?")[0],
        "title": data.get("name") or key,
        "published_on": published,
        "kind": args.kind,
        "topic": args.topic,
        "briefing": args.briefing,
        "annex_slug": args.annex,
        "price": data.get("price") or 0,
        "memo": args.memo,
    })
    record_article_stats(conn, article_id, data.get("like_count") or 0, data.get("comment_count") or 0)
    if args.briefing:
        set_backlog_state(conn, args.briefing, "published", article_id=article_id)
    print(f"registered #{article_id}: {data.get('name')}")
    print(f"  {published}  スキ {data.get('like_count')}  {'有料' if data.get('price') else '無料'}  kind={args.kind}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    conn = connect()
    articles = list_articles(conn)
    if not articles:
        print("no articles registered yet")
        return 0
    for a in articles:
        if not a["note_key"]:
            continue
        try:
            data = fetch_note(a["note_key"])
        except Exception as exc:  # noqa: BLE001 — one dead article shouldn't stop the sync
            print(f"  {a['note_key']}: {exc}", file=sys.stderr)
            continue
        likes = data.get("like_count") or 0
        comments = data.get("comment_count") or 0
        record_article_stats(conn, a["article_id"], likes, comments)
        delta = likes - (a["likes"] or 0)
        print(f"  {a['published_on']} スキ {likes:4}{f' (+{delta})' if delta > 0 else ''}  {a['title'][:40]}")
        time.sleep(args.delay)
    return 0


def challenge_progress(conn) -> dict:
    """Streak and pace inside the challenge window.

    The window starts at meta.challenge_start; without it, at the first day of
    the current unbroken streak, so articles from before the challenge (or a
    one-off from months ago) don't distort the count.
    """
    articles = list_articles(conn)
    days = sorted({a["published_on"] for a in articles if a["published_on"]})
    if not days:
        return {"published": 0, "posted_days": 0, "streak": 0, "start": None,
                "day_number": 0, "remaining": CHALLENGE_LENGTH, "today_done": False,
                "missing_days": [], "by_kind": {}}
    today = date.today()
    day_set = {date.fromisoformat(d) for d in days}

    streak, cursor = 0, today if today in day_set else today - timedelta(days=1)
    while cursor in day_set:
        streak += 1
        cursor -= timedelta(days=1)
    streak_start = cursor + timedelta(days=1) if streak else today

    configured = get_meta(conn, "challenge_start")
    start = date.fromisoformat(configured) if configured else streak_start
    window = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    posted = [d for d in window if d in day_set]
    in_window = [a for a in articles if a["published_on"] and start.isoformat() <= a["published_on"]]
    return {
        "published": len(in_window),
        "posted_days": len(posted),
        "streak": streak,
        "start": start.isoformat(),
        "day_number": len(window),
        "remaining": max(0, CHALLENGE_LENGTH - len(posted)),
        "today_done": today in day_set,
        "missing_days": [d.isoformat() for d in window if d not in day_set][-10:],
        "by_kind": {
            k: sum(1 for a in in_window if (a["kind"] or "other") == k)
            for k in KINDS
            if any((a["kind"] or "other") == k for a in in_window)
        },
        "total_articles": len(articles),
    }


def cmd_start(args: argparse.Namespace) -> int:
    conn = connect()
    if args.date:
        set_meta(conn, "challenge_start", args.date)
        print(f"challenge_start = {args.date}")
    else:
        print("challenge_start =", get_meta(conn, "challenge_start") or "(未設定: 連続記録の開始日を使います)")
    return cmd_streak(args)


def cmd_list(_args: argparse.Namespace) -> int:
    conn = connect()
    for a in list_articles(conn):
        growth = ""
        if a["likes"] is not None and a["likes_24h_ago"] is not None:
            diff = a["likes"] - a["likes_24h_ago"]
            growth = f" (24h +{diff})" if diff > 0 else ""
        print(f"{a['published_on']}  スキ {a['likes'] or 0:4}{growth:10} [{a['kind'] or '-':8}] {a['title'][:50]}")
    return 0


def cmd_streak(_args: argparse.Namespace) -> int:
    p = challenge_progress(connect())
    if not p["published"]:
        print("まだ記事が登録されていません (articles.py add <url>)")
        return 0
    print(f"100日チャレンジ (開始 {p['start']}): {p['day_number']}日目 / 投稿 {p['published']}本 ({p['posted_days']}日ぶん)")
    print(f"  連続: {p['streak']}日  今日: {'✅ 投稿済み' if p['today_done'] else '⬜ まだ'}  残り {p['remaining']}日")
    if p["by_kind"]:
        print("  型の内訳:", ", ".join(f"{k}={v}" for k, v in p["by_kind"].items()))
    if p["missing_days"]:
        print("  抜けた日 (直近):", ", ".join(p["missing_days"]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="register a published note article")
    p.add_argument("url")
    p.add_argument("--kind", choices=KINDS, default="other")
    p.add_argument("--topic")
    p.add_argument("--briefing")
    p.add_argument("--annex")
    p.add_argument("--memo")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("import", help="pull every published note of a creator")
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--max-pages", type=int, default=10)
    p.add_argument("--delay", type=float, default=0.6)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("set", help="attach kind / topic / briefing to an article")
    p.add_argument("url", help="note URL or key")
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--topic")
    p.add_argument("--briefing")
    p.add_argument("--annex")
    p.add_argument("--memo")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("sync", help="refresh スキ / comments for every article")
    p.add_argument("--delay", type=float, default=0.6)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("start", help="set / show the challenge start date")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.set_defaults(func=cmd_start)

    sub.add_parser("list", help="list registered articles").set_defaults(func=cmd_list)
    sub.add_parser("streak", help="challenge progress").set_defaults(func=cmd_streak)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
