#!/usr/bin/env python3
"""Term classification runner.

Reads unclassified terms from the term DB, batches them, and invokes Claude
to classify each as PROPER_PRODUCT / PROPER_ORG / PROPER_PERSON / PROPER_PLACE
/ PROPER_WORK / CONCEPT / GENERIC / JUNK.

Two subcommands:

  populate  — sync terms from the current signal corpus into terms.db
  import    — import previously-produced classification JSON files into the DB
              (useful when classification was done via subagents in-session)
  run       — batch pending terms and call `claude -p` for each batch
              (requires an up-to-date `claude` CLI; skip if it errors)

The three phases are decoupled so classification can happen out-of-band while
the rest of the pipeline still works.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from analyze_signals import build_index_and_term_sets, load_signals  # noqa: E402
from llm import call_claude_json  # noqa: E402
from term_db import (  # noqa: E402
    LABELS,
    connect,
    list_unclassified,
    save_classifications_bulk,
    stats,
    upsert_terms,
)


CLASSIFY_PROMPT = """Classify each term below into exactly ONE label. The terms
come from Japanese/English tech news, arXiv abstracts, HN posts, Zenn/Qiita
articles, and Bluesky posts.

Labels:
- PROPER_PRODUCT — specific software, model, framework, API, product (Claude, GPT-5, Ollama, LangChain, Cursor, PyTorch)
- PROPER_ORG — company / lab / team / foundation / university (Anthropic, OpenAI, 松尾研, 東京大学)
- PROPER_PERSON — a person's name
- PROPER_PLACE — geographic entity
- PROPER_WORK — paper / book / event / standard / protocol name (MCP, RFC 8707, ICML 2026)
- CONCEPT — technical concept or well-defined method (prompt injection, 強化学習, transformer)
- GENERIC — common noun with no specific referent (framework, performance, データ)
- JUNK — tokenization artifact, non-word, unusable

Output ONLY a JSON array (no markdown, no preamble):
[{"term": "<same string>", "label": "<LABEL>", "confidence": 0.0-1.0}, ...]

Include confidence only if <0.9 (uncertain); omit when confident.

Terms:
"""


def cmd_populate(args: argparse.Namespace) -> int:
    signals = load_signals()
    idx, _ = build_index_and_term_sets(signals, noun_mode="all")
    conn = connect()
    term_to_occ = {t: s.count for t, s in idx.items() if s.count >= args.min_occurrence}
    new, upd = upsert_terms(conn, term_to_occ)
    print(f"populated: {new} new, {upd} updated (min_occurrence={args.min_occurrence})")
    print("stats:", stats(conn))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    conn = connect()
    total = 0
    invalid_labels = set()
    for path in args.files:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cleaned = []
        for row in data:
            label = str(row.get("label", "")).upper().strip()
            if label not in LABELS:
                invalid_labels.add(label)
                continue
            cleaned.append({
                "term": row["term"],
                "label": label,
                "confidence": row.get("confidence"),
                "rationale": row.get("rationale"),
            })
        n = save_classifications_bulk(conn, cleaned, model=args.model)
        total += n
        print(f"{path}: imported {n}/{len(data)}")
    if invalid_labels:
        print(f"warn: dropped rows with unknown labels: {sorted(invalid_labels)}", file=sys.stderr)
    print(f"total: {total} classifications imported")
    print("stats:", stats(conn))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    conn = connect()
    pending = list_unclassified(conn, min_occurrence=args.min_occurrence, limit=args.limit)
    if not pending:
        print("no unclassified terms")
        return 0
    print(f"pending: {len(pending)} terms (batch size {args.batch_size})")
    total = 0
    for i in range(0, len(pending), args.batch_size):
        batch = pending[i:i + args.batch_size]
        prompt = CLASSIFY_PROMPT + "\n".join(f"- {t} ({o} occurrences, {lang})" for t, o, lang in batch)
        try:
            data = call_claude_json(prompt, timeout=180)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 — report and skip the batch
            print(f"batch {i//args.batch_size}: {exc}", file=sys.stderr)
            continue
        cleaned = [
            {
                "term": row["term"],
                "label": str(row["label"]).upper().strip(),
                "confidence": row.get("confidence"),
            }
            for row in data
            if row.get("label", "").upper() in LABELS
        ]
        n = save_classifications_bulk(conn, cleaned, model="claude-cli")
        total += n
        print(f"batch {i//args.batch_size}: {n}/{len(batch)} saved")
    print(f"done: {total} classifications saved")
    print("stats:", stats(conn))
    return 0


def cmd_stats(_args: argparse.Namespace) -> int:
    conn = connect()
    s = stats(conn)
    print(f"total terms: {s['total_terms']}")
    print(f"classified:  {s['classified']} ({100 * s['classified'] / max(1, s['total_terms']):.1f}%)")
    for label, count in s["by_label"].items():
        print(f"  {label:20} {count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pop = sub.add_parser("populate", help="Upsert terms from current signals into DB")
    p_pop.add_argument("--min-occurrence", type=int, default=2)
    p_pop.set_defaults(func=cmd_populate)

    p_imp = sub.add_parser("import", help="Import classification JSON produced by subagents")
    p_imp.add_argument("files", nargs="+")
    p_imp.add_argument("--model", default="subagent")
    p_imp.set_defaults(func=cmd_import)

    p_run = sub.add_parser("run", help="Call `claude -p` to classify pending terms (needs recent CLI)")
    p_run.add_argument("--batch-size", type=int, default=100)
    p_run.add_argument("--min-occurrence", type=int, default=3)
    p_run.add_argument("--limit", type=int, default=None)
    p_run.set_defaults(func=cmd_run)

    p_st = sub.add_parser("stats", help="Show DB stats")
    p_st.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
