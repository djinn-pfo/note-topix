#!/usr/bin/env python3
"""LLM preparation for the buffer-overlap network (scripts/buffer_edges.py).

Two judgments, both cached in terms.db and preserved across pipeline runs:

  aliases — group proper-noun variants under one canonical term
            ("hokusai", "katsushika" -> "katsushika hokusai"). One global
            pass over all cores; the alias table is replaced wholesale.
  vocab   — mark which CONCEPT / GENERIC nouns are topic-bearing enough to be
            buffer features (ok) vs filler / boilerplate / template (drop).
            Incremental: only unjudged terms are sent unless --redo.

    uv run scripts/buffer_prep.py aliases run [--if-changed]
    uv run scripts/buffer_prep.py aliases import groups.json
    uv run scripts/buffer_prep.py aliases show
    uv run scripts/buffer_prep.py vocab run [--batch-size 150] [--workers 4]
    uv run scripts/buffer_prep.py vocab import judgments.json
    uv run scripts/buffer_prep.py vocab stats

Import formats (for out-of-band subagent runs):
  aliases: [{"canonical": "katsushika hokusai", "aliases": ["hokusai", "katsushika"]}, ...]
  vocab:   {"<term>": true | false, ...}
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from term_db import (  # noqa: E402
    PROPER_LABELS,
    connect,
    get_aliases,
    get_buffer_vocab,
    get_classifications,
    get_meta,
    members_hash,
    read_term_stats,
    replace_aliases,
    set_meta,
    save_buffer_vocab_bulk,
)
from buffer_edges import BUFFER_LABELS  # noqa: E402
from llm import call_claude_json  # noqa: E402

ALIASES_PROMPT = """Below are proper nouns extracted (lower-cased, sometimes
split or partially joined by a tokenizer) from Japanese/English tech news,
arXiv abstracts, Hacker News, Zenn/Qiita, Bluesky and Google Trends.

Group terms that refer to the SAME real-world entity, and pick a canonical
term for each group. Merge:
  - spelling / case / plural variants, EN ↔ JA names (tokyo / 東京, japan / 日本)
  - a person's partial names and full name (hokusai, katsushika → katsushika hokusai)
  - "X" and "X prefecture" / "mount X" / "X県" when they denote the same place
  - a publisher and its news brand (産経 / 産経ニュース)
  - a short name and the full name of the same product (opus / claude opus)
Do NOT merge:
  - a product and a distinct sub-product or sibling (claude vs claude code, gpt-6 vs gpt-5.6)
  - an organization and its product (openai vs chatgpt, yahoo! vs yahoo!ニュース is a service: keep separate)
  - different model versions / generations (qwen2.5 vs qwen3)
Use the context snippets to check what each term actually refers to in this
corpus (e.g. 富士 inside a sumo wrestler's name is not Mount Fuji).
When unsure, do not merge.

The canonical MUST be one of the listed terms (prefer the most complete,
recognizable form). Only output groups with 2+ members.

Output ONLY a JSON array (no markdown, no preamble):
[{"canonical": "<term>", "aliases": ["<other term>", ...]}, ...]

Terms (term — label, signals — context snippets):
"""

VOCAB_PROMPT = """We build a network of proper nouns by comparing the common
nouns that appear right next to them ("buffer" words). A good buffer word
tells you WHAT the proper noun is being discussed about — its topic or angle.

From the terms below, select the ones to KEEP as buffer words.

KEEP nouns that name a topic, domain, activity, object, issue, or technical
concept: agent, alignment, inference, robotics, pricing, regulation,
copyright, souvenir, pennant, 推論, 価格, 規制, 著作権, 相撲, 選挙, 観光.

DROP:
  - filler with no topical content: time, number, way, end, set, part, case,
    type, example, thing, 感じ, 種類, 結果, 方法, 一番, 今回, 場合
  - research/report boilerplate: approach, results, experiments, baseline,
    evaluation, performance, analysis, framework, method, state, art, gains
  - quantities, degrees, units: hundreds, percent, rate, times
  - web/page template words: menu, excerpt, watch, share, 配信, メニュー
  - fragments, typos, tokenizer debris

Output ONLY a JSON array of the KEPT terms, copied exactly (no markdown):
["<term>", ...]

Terms:
"""


# ---------- aliases ----------


def _core_terms(conn) -> list[tuple[str, str, int]]:
    stats = read_term_stats(conn, "all")
    rows = [
        (t, meta["label"], stats.get(t, {}).get("occurrence", 0))
        for t, meta in get_classifications(conn).items()
        if meta["label"] in PROPER_LABELS
    ]
    rows.sort(key=lambda r: -r[2])
    return rows


def _snippets(conn, term: str, k: int = 2, width: int = 40) -> list[str]:
    rows = conn.execute(
        """
        SELECT s.title, s.body FROM signal_terms st JOIN signals s USING(signal_id)
         WHERE st.term = ? AND st.noun_mode = 'all' LIMIT 20
        """,
        (term,),
    ).fetchall()
    out: list[str] = []
    for title, body in rows:
        text = f"{title or ''} / {body or ''}"
        i = text.lower().find(term.lower())
        if i < 0:
            continue
        snippet = text[max(0, i - width):i + len(term) + width].replace("\n", " ")
        if snippet not in out:
            out.append(snippet)
        if len(out) >= k:
            break
    return out


def _validate_groups(groups: list[dict], known: set[str]) -> list[dict]:
    cleaned, claimed = [], set()
    for g in groups:
        canonical = g.get("canonical")
        members = [a for a in g.get("aliases", []) if a in known and a != canonical and a not in claimed]
        if canonical not in known or not members:
            print(f"  skip group {g!r}", file=sys.stderr)
            continue
        claimed.update(members)
        cleaned.append({"canonical": canonical, "aliases": members})
    return cleaned


def cmd_aliases_run(args: argparse.Namespace) -> int:
    conn = connect()
    cores = _core_terms(conn)
    core_set_hash = members_hash([t for t, _, _ in cores])
    if args.if_changed and get_meta(conn, "aliases_core_set") == core_set_hash:
        print("aliases: core set unchanged since last grouping, skipped")
        return 0
    prompt = ALIASES_PROMPT + "\n".join(
        f"- {t} — {label}, {occ} — " + " | ".join(f"「{x}」" for x in _snippets(conn, t))
        for t, label, occ in cores
    )
    print(f"grouping {len(cores)} proper nouns via claude -p ...")
    groups = _validate_groups(call_claude_json(prompt, args.model), {t for t, _, _ in cores})
    if args.dry_run:
        print(json.dumps(groups, ensure_ascii=False, indent=1))
        return 0
    n = replace_aliases(conn, groups, model=args.model or "claude-cli")
    set_meta(conn, "aliases_core_set", core_set_hash)
    print(f"saved {n} aliases in {len(groups)} groups")
    return cmd_aliases_show(args)


def cmd_aliases_import(args: argparse.Namespace) -> int:
    conn = connect()
    known = {t for t, _, _ in _core_terms(conn)}
    groups = _validate_groups(json.loads(Path(args.file).read_text(encoding="utf-8")), known)
    n = replace_aliases(conn, groups, model=args.model)
    set_meta(conn, "aliases_core_set", members_hash(sorted(known)))
    print(f"imported {n} aliases in {len(groups)} groups")
    return 0


def cmd_aliases_show(_args: argparse.Namespace) -> int:
    by_canonical: dict[str, list[str]] = {}
    for alias, canonical in get_aliases(connect()).items():
        by_canonical.setdefault(canonical, []).append(alias)
    for canonical, aliases in sorted(by_canonical.items()):
        print(f"  {canonical}  <=  {', '.join(sorted(aliases))}")
    return 0


# ---------- vocab ----------


def _vocab_candidates(conn) -> list[str]:
    stats = read_term_stats(conn, "all")
    terms = [t for t, meta in get_classifications(conn).items() if meta["label"] in BUFFER_LABELS]
    terms.sort(key=lambda t: -stats.get(t, {}).get("occurrence", 0))
    return terms


def cmd_vocab_run(args: argparse.Namespace) -> int:
    conn = connect()
    judged = get_buffer_vocab(conn)
    pending = [t for t in _vocab_candidates(conn) if args.redo or t not in judged]
    if args.limit:
        pending = pending[: args.limit]
    if not pending:
        print("no unjudged buffer candidates")
        return 0
    batches = [pending[i:i + args.batch_size] for i in range(0, len(pending), args.batch_size)]
    print(f"judging {len(pending)} terms in {len(batches)} batches ({args.workers} parallel)")

    def run(batch: list[str]) -> dict[str, bool] | None:
        try:
            kept = call_claude_json(VOCAB_PROMPT + "\n".join(f"- {t}" for t in batch), args.model)
        except Exception as exc:  # noqa: BLE001 — report and skip the batch
            print(f"  batch failed: {exc}", file=sys.stderr)
            return None
        kept_set = {str(k) for k in kept}
        return {t: t in kept_set for t in batch}

    total = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, result in enumerate(pool.map(run, batches)):
            if result is None:
                continue
            save_buffer_vocab_bulk(conn, result, model=args.model or "claude-cli")
            total += len(result)
            print(f"  batch {i}: kept {sum(result.values())}/{len(result)}")
    print(f"saved {total} judgments")
    return cmd_vocab_stats(args)


def cmd_vocab_import(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    n = save_buffer_vocab_bulk(connect(), {str(k): bool(v) for k, v in data.items()}, model=args.model)
    print(f"imported {n} judgments")
    return 0


def cmd_vocab_stats(_args: argparse.Namespace) -> int:
    conn = connect()
    judged = get_buffer_vocab(conn)
    candidates = _vocab_candidates(conn)
    kept = [t for t in candidates if judged.get(t)]
    print(f"candidates: {len(candidates)}  judged: {sum(1 for t in candidates if t in judged)}  kept: {len(kept)}")
    print("  top kept:", ", ".join(kept[:40]))
    print("  top dropped:", ", ".join([t for t in candidates if judged.get(t) is False][:40]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="group", required=True)

    p_al = sub.add_parser("aliases").add_subparsers(dest="cmd", required=True)
    p = p_al.add_parser("run")
    p.add_argument("--model")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--if-changed", action="store_true", help="skip when no proper noun was added/removed since the last run")
    p.set_defaults(func=cmd_aliases_run)
    p = p_al.add_parser("import")
    p.add_argument("file")
    p.add_argument("--model", default="subagent")
    p.set_defaults(func=cmd_aliases_import)
    p_al.add_parser("show").set_defaults(func=cmd_aliases_show)

    p_vo = sub.add_parser("vocab").add_subparsers(dest="cmd", required=True)
    p = p_vo.add_parser("run")
    p.add_argument("--model")
    p.add_argument("--batch-size", type=int, default=150)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int)
    p.add_argument("--redo", action="store_true", help="re-judge already judged terms")
    p.set_defaults(func=cmd_vocab_run)
    p = p_vo.add_parser("import")
    p.add_argument("file")
    p.add_argument("--model", default="subagent")
    p.set_defaults(func=cmd_vocab_import)
    p_vo.add_parser("stats").set_defaults(func=cmd_vocab_stats)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
