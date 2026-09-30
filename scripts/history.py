#!/usr/bin/env python3
"""Time-series history of the buffer-overlap network.

run_pipeline.py calls ``record_snapshot`` at the end of every run (after the
LLM stage, so fresh community names are included). Each run appends:

  pipeline_runs        one row per run
  community_snapshots  every community under the UI's default buffer view
  core_snapshots       every proper-noun core, with the lineage it sat in

Community identity across runs is a *lineage*: a snapshot joins the lineage
whose most recent snapshot (within --lookback-runs) overlaps it most, if the
member Jaccard is >= LINEAGE_JACCARD; otherwise it starts a new lineage.
Matching is one-to-one, strongest overlap first, so a split keeps the lineage
on the larger-overlap side.

    uv run scripts/history.py record     # snapshot the current DB state
    uv run scripts/history.py show       # lineages with their trajectory
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from buffer_edges import DEFAULT_VIEW, BufferConfig, build_network  # noqa: E402
from term_db import (  # noqa: E402
    connect,
    get_meta,
    jaccard,
    members_hash,
    read_community_name,
)

LINEAGE_JACCARD = 0.5
LOOKBACK_RUNS = 4  # = one day at the 6-hour cron cadence


def _recent_lineage_members(conn, before_run_id: int, lookback: int) -> dict[int, list[str]]:
    """{lineage_id: members of its latest snapshot} over the last N runs."""
    rows = conn.execute(
        """
        SELECT cs.lineage_id, cs.members_json
          FROM community_snapshots cs
          JOIN (SELECT lineage_id, MAX(run_id) AS run_id
                  FROM community_snapshots
                 WHERE run_id < ? AND run_id >= ?
                 GROUP BY lineage_id) latest
            ON latest.lineage_id = cs.lineage_id AND latest.run_id = cs.run_id
        """,
        (before_run_id, before_run_id - lookback),
    ).fetchall()
    return {lid: json.loads(m) for lid, m in rows}


def record_snapshot(conn, lookback: int = LOOKBACK_RUNS) -> int | None:
    """Append one snapshot for the DB's current pipeline state. Returns the run_id,
    or None if this pipeline run was already recorded."""
    run_at = get_meta(conn, "last_run_at")
    if not run_at:
        raise RuntimeError("no pipeline run in DB (meta.last_run_at missing)")
    if conn.execute("SELECT 1 FROM pipeline_runs WHERE run_at = ?", (run_at,)).fetchone():
        return None
    signal_count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    run_id = conn.execute(
        "INSERT INTO pipeline_runs(run_at, signal_count, view_json) VALUES (?,?,?)",
        (run_at, signal_count, json.dumps(DEFAULT_VIEW)),
    ).lastrowid

    view = DEFAULT_VIEW
    net = build_network(conn, BufferConfig(window=view["window"], include_verbs=view["include_verbs"]))
    _, _, node_to_com = net.partition(view["min_occurrence"], view["min_weight"], view["max_nodes"])
    by_com: dict[int, list[str]] = {}
    for node, cid in node_to_com.items():
        by_com.setdefault(cid, []).append(node)
    communities = [sorted(m) for m in by_com.values()]

    # One-to-one lineage matching, strongest overlap first.
    previous = _recent_lineage_members(conn, run_id, lookback)
    pairs = sorted(
        ((jaccard(members, prev), i, lid)
         for i, members in enumerate(communities)
         for lid, prev in previous.items()),
        reverse=True,
    )
    assigned: dict[int, int] = {}
    taken: set[int] = set()
    for score, i, lid in pairs:
        if score < LINEAGE_JACCARD:
            break
        if i in assigned or lid in taken:
            continue
        assigned[i] = lid
        taken.add(lid)

    term_lineage: dict[str, int] = {}
    for i, members in enumerate(communities):
        mhash = members_hash(members)
        named = read_community_name(conn, mhash)
        name = named["name"] if named else None
        lid = assigned.get(i)
        if lid is None:
            lid = conn.execute(
                "INSERT INTO community_lineages(first_run_id, last_run_id, name) VALUES (?,?,?)",
                (run_id, run_id, name),
            ).lastrowid
        else:
            conn.execute(
                "UPDATE community_lineages SET last_run_id = ?, name = COALESCE(?, name) WHERE lineage_id = ?",
                (run_id, name, lid),
            )
        signals = set().union(*(net.cores[m].signals for m in members))
        buckets = {net.corpus.bucket.get(s, "") for s in signals}
        conn.execute(
            """
            INSERT INTO community_snapshots(run_id, lineage_id, members_hash, name, size,
                signal_count, bucket_count, members_json, buffers_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id, lid, mhash, name, len(members), len(signals), len(buckets),
                json.dumps(members, ensure_ascii=False),
                json.dumps([f for f, _ in net.community_buffers(members)], ensure_ascii=False),
            ),
        )
        for m in members:
            term_lineage[m] = lid

    conn.executemany(
        """
        INSERT INTO core_snapshots(run_id, term, occurrence, mentions, buckets, lineage_id)
        VALUES (?,?,?,?,?,?)
        """,
        [
            (
                run_id, t, len(core.signals), core.mentions,
                ",".join(sorted({net.corpus.bucket.get(s, "") for s in core.signals})),
                term_lineage.get(t),
            )
            for t, core in net.cores.items()
        ],
    )
    conn.commit()
    return run_id


def backfill_note_coverage(conn) -> int:
    """Seed note_coverage_history from the latest-only note_coverage table."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO note_coverage_history(term, fetched_at, total)
        SELECT term, fetched_at, total FROM note_coverage
        """
    )
    conn.commit()
    return cur.rowcount


def cmd_record(args: argparse.Namespace) -> int:
    conn = connect()
    seeded = backfill_note_coverage(conn)
    if seeded:
        print(f"note_coverage_history: seeded {seeded} rows")
    run_id = record_snapshot(conn, args.lookback_runs)
    if run_id is None:
        print("this pipeline run is already recorded")
        return 0
    n_com, n_new = conn.execute(
        """
        SELECT COUNT(*), SUM(l.first_run_id = ?) FROM community_snapshots cs
          JOIN community_lineages l USING (lineage_id) WHERE cs.run_id = ?
        """,
        (run_id, run_id),
    ).fetchone()
    print(f"recorded run {run_id}: {n_com} communities ({n_new} new lineages)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    conn = connect()
    runs = dict(conn.execute("SELECT run_id, run_at FROM pipeline_runs"))
    rows = conn.execute(
        """
        SELECT l.lineage_id, l.name, COUNT(*) AS n_runs,
               MIN(cs.run_id), MAX(cs.run_id)
          FROM community_lineages l JOIN community_snapshots cs USING (lineage_id)
         GROUP BY l.lineage_id ORDER BY n_runs DESC, MAX(cs.run_id) DESC
        """
    ).fetchall()
    print(f"runs recorded: {len(runs)}  lineages: {len(rows)}")
    for lid, name, n_runs, first, last in rows[: args.top]:
        traj = conn.execute(
            "SELECT signal_count, size FROM community_snapshots WHERE lineage_id = ? ORDER BY run_id",
            (lid,),
        ).fetchall()
        members = Counter()
        for (m,) in conn.execute("SELECT members_json FROM community_snapshots WHERE lineage_id = ?", (lid,)):
            members.update(json.loads(m))
        print(
            f"  L{lid:<3} {name or '(unnamed)':28} runs={n_runs:<3} "
            f"{runs[first][:13]} → {runs[last][:13]}  "
            f"signals {traj[0][0]}→{traj[-1][0]}  members: {', '.join(t for t, _ in members.most_common(6))}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record", help="snapshot the current pipeline state")
    p.add_argument("--lookback-runs", type=int, default=LOOKBACK_RUNS)
    p.set_defaults(func=cmd_record)
    p = sub.add_parser("show", help="list community lineages")
    p.add_argument("--top", type=int, default=30)
    p.set_defaults(func=cmd_show)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
