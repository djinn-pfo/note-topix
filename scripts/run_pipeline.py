#!/usr/bin/env python3
"""Full pipeline runner intended for cron/systemd scheduling.

Steps:
  1. Run all fetch_*.py scripts (skip on --skip-fetch)
  2. Load signals, tokenize (noun_mode='all' and 'propn') + positioned slots
  3. Compute edges for every (mode, method) combination
  4. Upsert new terms into the term DB (keeps existing classifications)
  5. Refresh note.com paid-note coverage for top-N terms per mode
  6. Persist everything to knowledges/terms.db + record last_run_at
  7. LLM enrichment via `claude -p` (skip with --skip-llm), each step
     incremental so a quiet 6-hour window costs almost nothing:
       classify new terms -> judge new buffer vocab -> regroup aliases if the
       proper-noun set changed -> name new buffer-network communities ->
       write briefings for new communities (max 3 per run)
  8. Append a time-series snapshot (scripts/history.py): per-run community
     lineages + core stats, never wiped, for deep-dive candidate detection.
     Also refresh own published articles / スキ counts (scripts/articles.py)

The dashboard reads exclusively from this DB, so cold start is instant.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from analyze_signals import (  # noqa: E402
    TermExtractor,
    _pair_and_term_counts,
    build_index_and_term_sets,
    edge_weights,
    load_signals,
    note_coverage,
    rank_terms,
    term_text,
)
from history import record_snapshot  # noqa: E402
from llm import health_check  # noqa: E402
from term_db import (  # noqa: E402
    clear_pipeline_data,
    connect,
    persist_edges,
    persist_note_coverage,
    persist_signal_slots,
    persist_signal_terms,
    persist_signal_verbs,
    persist_signals,
    persist_term_stats,
    set_meta,
    upsert_terms,
)

FETCHERS = [
    "fetch_raindrop.py",
    "fetch_hn.py",
    "fetch_arxiv.py",
    "fetch_zenn.py",
    "fetch_qiita.py",
    "fetch_bluesky.py",
    "fetch_google_trends.py",
]
NOUN_MODES = ("all", "propn")
EDGE_METHODS = ("npmi", "pmi", "jaccard", "count")
MIN_PAIR_COUNT = 2
LLM_STEP_TIMEOUT_SECONDS = 1800
NOTE_COVERAGE_TOP_N = 100          # per mode; total unique <= 2 * this
NOTE_COVERAGE_DELAY_SECONDS = 0.4  # be polite to note.com


def _run_fetch(name: str) -> tuple[str, int, str]:
    path = SCRIPTS_DIR / name
    proc = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=300,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
    return name, proc.returncode, tail[-1]


def run_fetchers() -> dict[str, str]:
    print("[1/8] fetchers")
    results: dict[str, str] = {}
    for name in FETCHERS:
        started = time.monotonic()
        try:
            n, code, summary = _run_fetch(name)
        except subprocess.TimeoutExpired:
            print(f"  {name:24} TIMEOUT")
            results[name] = "timeout"
            continue
        elapsed = time.monotonic() - started
        marker = "OK " if code == 0 else "ERR"
        print(f"  {marker} {name:24} {elapsed:5.1f}s  {summary}")
        results[name] = "ok" if code == 0 else f"exit {code}"
    return results


# Order matters: new terms need labels before they can be cores/buffers, and
# communities need names before they get briefings.
LLM_STEPS = [
    ("classify new terms", ["classify_terms.py", "run", "--min-occurrence", "3"]),
    ("judge buffer vocab", ["buffer_prep.py", "vocab", "run"]),
    ("regroup aliases", ["buffer_prep.py", "aliases", "run", "--if-changed"]),
    ("name communities", ["name_communities.py", "run"]),
    ("auto briefings", ["generate_briefing.py", "auto"]),
]


def run_llm_stage(max_briefings: int) -> None:
    """Each step is a separate process; a failure is logged and the chain
    continues, since later steps still work on the previous state."""
    error = health_check()
    if error:
        print(f"  claude -p health check failed, skipping LLM steps: {error}")
        return
    for label, argv in LLM_STEPS:
        if argv[0] == "generate_briefing.py":
            argv = [*argv, "--max", str(max_briefings)]
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / argv[0]), *argv[1:]],
                capture_output=True,
                text=True,
                timeout=LLM_STEP_TIMEOUT_SECONDS,
                cwd=REPO_ROOT,
            )
            status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
            output = (result.stdout + result.stderr).strip()
        except subprocess.TimeoutExpired:
            status, output = "timeout", ""
        print(f"  {label}: {status} ({time.monotonic() - t0:.0f}s)")
        for line in output.splitlines()[-12:]:
            print(f"    {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-note-coverage", action="store_true")
    parser.add_argument("--note-top-n", type=int, default=NOTE_COVERAGE_TOP_N)
    parser.add_argument("--skip-llm", action="store_true", help="skip step 7 (claude -p enrichment)")
    parser.add_argument("--max-briefings", type=int, default=3)
    args = parser.parse_args()

    t_start = time.monotonic()
    conn = connect()

    fetch_summary: dict[str, str] = {}
    if not args.skip_fetch:
        fetch_summary = run_fetchers()
    else:
        print("[1/8] fetchers skipped")

    print("[2/8] loading signals")
    signals = load_signals()
    if not signals:
        print("no signals — nothing to persist")
        return 1
    print(f"  signals loaded: {len(signals)}")

    print("[3/8] tokenizing + indexing")
    indices: dict[str, tuple] = {}
    for mode in NOUN_MODES:
        t0 = time.monotonic()
        idx, term_sets = build_index_and_term_sets(signals, noun_mode=mode)
        elapsed = time.monotonic() - t0
        indices[mode] = (idx, term_sets)
        print(f"  mode={mode:5} terms={len(idx):5}  ({elapsed:.1f}s)")

    # Positioned slots (noun runs + verbs) for the buffer-overlap network.
    # Verbs are POS-agnostic, so the per-signal verb sets come from the same pass.
    t0 = time.monotonic()
    slot_extractor = TermExtractor(noun_mode="all")
    slot_lists = [slot_extractor.extract_slots(term_text(s)) for s in signals]
    verb_sets: list[set[str]] = [
        {sl.units[0] for sl in slots if sl.kind == "v"} for slots in slot_lists
    ]
    print(
        f"  slots: {sum(len(x) for x in slot_lists)}  "
        f"verbs: {sum(len(v) for v in verb_sets)} occurrences across signals  "
        f"({time.monotonic() - t0:.1f}s)"
    )

    print("[4/8] computing edges")
    edges_bundle: dict[tuple[str, str], tuple] = {}
    for mode in NOUN_MODES:
        _, term_sets = indices[mode]
        pair_counts, _ = _pair_and_term_counts(term_sets)
        for method in EDGE_METHODS:
            t0 = time.monotonic()
            edges, _ = edge_weights(term_sets, method=method, min_pair_count=MIN_PAIR_COUNT)
            edges_bundle[(mode, method)] = (edges, pair_counts)
            print(
                f"  mode={mode:5} method={method:8} edges={len(edges):6}  "
                f"({time.monotonic() - t0:.2f}s)"
            )

    print("[5/8] refreshing note.com coverage (top terms across modes)")
    coverage_terms: list[str] = []
    if not args.skip_note_coverage:
        top_set: set[str] = set()
        for mode in NOUN_MODES:
            idx, _ = indices[mode]
            for term, _ in rank_terms(idx)[: args.note_top_n]:
                top_set.add(term)
        coverage_terms = sorted(top_set)
        print(f"  fetching coverage for {len(coverage_terms)} unique terms...")
        for i, term in enumerate(coverage_terms, start=1):
            cov = note_coverage(term)
            persist_note_coverage(conn, term, cov)
            time.sleep(NOTE_COVERAGE_DELAY_SECONDS)
            if i % 25 == 0:
                print(f"    {i}/{len(coverage_terms)}")
    else:
        print("  note.com coverage skipped")

    print("[6/8] persisting to term DB")
    # Wipe derived tables, but keep `terms` + `classifications` (LLM investment).
    clear_pipeline_data(conn)
    path_to_id = persist_signals(conn, signals)

    # Refresh `terms` occurrence from 'all' mode (broadest count) for the
    # classification pool; classification stays keyed by term string.
    all_idx, _ = indices["all"]
    upsert_terms(conn, {t: s.count for t, s in all_idx.items() if s.count >= 2})

    for mode in NOUN_MODES:
        idx, term_sets = indices[mode]
        persist_term_stats(conn, mode, idx)
        persist_signal_terms(conn, mode, signals, term_sets, path_to_id)

    persist_signal_verbs(conn, signals, verb_sets, path_to_id)
    persist_signal_slots(conn, signals, slot_lists, path_to_id)

    for (mode, method), (edges, pair_counts) in edges_bundle.items():
        persist_edges(conn, mode, method, edges, pair_counts)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    set_meta(conn, "last_run_at", now)
    set_meta(conn, "signal_count", str(len(signals)))
    set_meta(conn, "fetch_summary", json.dumps(fetch_summary, ensure_ascii=False))
    set_meta(conn, "note_coverage_terms", str(len(coverage_terms)))

    conn.close()
    if args.skip_llm:
        print("[7/8] LLM enrichment skipped")
    else:
        print("[7/8] LLM enrichment (claude -p)")
        run_llm_stage(args.max_briefings)

    # After the LLM stage so snapshots carry fresh community names.
    print("[8/8] recording snapshot + article stats")
    conn = connect()
    run_id = record_snapshot(conn)
    n_com = conn.execute("SELECT COUNT(*) FROM community_snapshots WHERE run_id = ?", (run_id,)).fetchone()[0]
    print(f"  run {run_id}: {n_com} communities")
    conn.close()
    # Own published articles: pick up new posts and re-read スキ counts, so the
    # 100-day challenge dashboard and the growth curves stay current.
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "articles.py"), "import", "--max-pages", "2"],
        capture_output=True, text=True, timeout=300, cwd=REPO_ROOT,
    )
    for line in (result.stdout + result.stderr).strip().splitlines()[-4:]:
        print(f"  {line}")

    elapsed = time.monotonic() - t_start
    print(f"pipeline done in {elapsed:.1f}s at {now}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
