#!/usr/bin/env python3
"""Topic backlog — which of the accumulated briefings are still worth writing.

The pipeline writes several briefings a day, so the folder grows faster than
anyone can write. This joins each briefing with:

  - its state (candidate / researched / writing / published / dropped)
  - **freshness**: is the topic still alive in the signal stream, or did it
    stop? News has a shelf life, and a dead topic is not worth a day of the
    challenge. Taken from the community lineage the briefing's topic belongs to.
  - the 「なぜ〇〇なのか」 questions it proposes, so a topic can be picked by
    the question rather than by the folder listing.

    uv run scripts/backlog.py list [--state candidate] [--fresh]
    uv run scripts/backlog.py set <briefing> <state> [--kind why] [--memo ...]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
BRIEFINGS_DIR = REPO_ROOT / "documents" / "briefings"

sys.path.insert(0, str(SCRIPTS_DIR))

from analyze_signals import parse_frontmatter  # noqa: E402
from term_db import connect, get_backlog, set_backlog_state  # noqa: E402

STATES = ("candidate", "researched", "writing", "published", "dropped")
# A topic whose lineage hasn't been seen for this long is treated as cooled off.
STALE_DAYS = 3
WHY_RE = re.compile(r"^-\s+\*\*(なぜ[^*]+)\*\*", re.MULTILINE)


def lineage_activity(conn) -> dict[str, dict]:
    """{community name: {last_seen, runs, signals_first, signals_last}}"""
    rows = conn.execute(
        """
        SELECT l.name,
               MAX(r.run_at), COUNT(*),
               MIN(cs.signal_count), MAX(cs.signal_count)
          FROM community_lineages l
          JOIN community_snapshots cs USING (lineage_id)
          JOIN pipeline_runs r ON r.run_id = cs.run_id
         WHERE l.name IS NOT NULL
         GROUP BY l.name
        """
    ).fetchall()
    return {
        name: {
            "last_seen": (last_seen or "")[:10],
            "runs": runs,
            "signals_first": first,
            "signals_last": last,
        }
        for name, last_seen, runs, first, last in rows
    }


def list_backlog(conn) -> list[dict]:
    states = get_backlog(conn)
    activity = lineage_activity(conn)
    today = date.today()
    out: list[dict] = []
    for path in sorted(BRIEFINGS_DIR.glob("*.md"), reverse=True):
        rel = str(path.relative_to(REPO_ROOT))
        fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        topic = str(fm.get("topic") or path.stem)
        info = states.get(rel, {})
        act = activity.get(topic, {})
        days_since = None
        if act.get("last_seen"):
            try:
                days_since = (today - date.fromisoformat(act["last_seen"])).days
            except ValueError:
                days_since = None
        out.append({
            "briefing": rel,
            "title": topic,
            "date": str(fm.get("date") or path.stem[:10]),
            "signal_count": fm.get("signal_count"),
            "why_questions": WHY_RE.findall(body),
            "state": info.get("state") or "candidate",
            "note_kind": info.get("note_kind"),
            "memo": info.get("memo"),
            "article_id": info.get("article_id"),
            "last_seen": act.get("last_seen"),
            "days_since_seen": days_since,
            "fresh": days_since is not None and days_since <= STALE_DAYS,
            "lineage_runs": act.get("runs"),
            "signal_growth": (
                (act.get("signals_last") or 0) - (act.get("signals_first") or 0)
                if act.get("signals_first") is not None else None
            ),
        })
    return out


def cmd_list(args: argparse.Namespace) -> int:
    conn = connect()
    rows = list_backlog(conn)
    if args.state:
        rows = [r for r in rows if r["state"] == args.state]
    if args.fresh:
        rows = [r for r in rows if r["fresh"]]
    if args.with_why:
        rows = [r for r in rows if r["why_questions"]]
    print(f"{len(rows)} briefings")
    for r in rows[: args.limit]:
        age = f"{r['days_since_seen']}日前" if r["days_since_seen"] is not None else "不明"
        mark = "🟢" if r["fresh"] else "⚪"
        print(f"{mark} [{r['state']:10}] {r['date']} {r['title'][:28]:30} 最終観測 {age:6} 問い {len(r['why_questions'])}")
        if args.verbose:
            for q in r["why_questions"]:
                print(f"      - {q}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    if args.state not in STATES:
        raise SystemExit(f"state must be one of {STATES}")
    set_backlog_state(connect(), args.briefing, args.state, args.kind, args.memo)
    print(f"{args.briefing} -> {args.state}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list")
    p.add_argument("--state", choices=STATES)
    p.add_argument("--fresh", action="store_true", help="only topics still active in the signal stream")
    p.add_argument("--with-why", action="store_true", help="only briefings that propose なぜ questions")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--verbose", "-v", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set")
    p.add_argument("briefing")
    p.add_argument("state", choices=STATES)
    p.add_argument("--kind")
    p.add_argument("--memo")
    p.set_defaults(func=cmd_set)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
