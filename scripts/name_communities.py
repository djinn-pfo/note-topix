#!/usr/bin/env python3
"""Community naming pipeline.

Runs Louvain on the co-occurrence graph for a chosen config, then, for each
community, collects (member terms, top co-occurring verbs, sample signal
titles) and hands the bundle to a Claude subagent for naming.

Subcommands:

  prepare  — compute communities and write per-community JSON prompts to
             /tmp/note-topix-communities/ so the caller can spawn subagents
  import   — take subagent output (JSON with community id + name + description)
             and store in the ``community_names`` table
  run      — unattended naming for the buffer-overlap network (``claude -p``).
             Uses the Network page's default buffer view so the stored hashes
             match what the UI shows. Already-named communities are skipped,
             and a community that only drifted slightly since it was named
             (member Jaccard >= --reuse-threshold) inherits the old name
             instead of costing another LLM call.

For ``--method buffer`` the bundle also carries the shared buffer features
(the nouns/verbs that tie members together), which are the main naming cue.

The dashboard reads ``community_names`` and shows the cached name/description
when the current Louvain partition matches a hashed member set.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import networkx as nx  # noqa: E402
from buffer_edges import DEFAULT_VIEW, VERB_PREFIX, BufferConfig, build_network  # noqa: E402
from llm import call_claude_json  # noqa: E402
from network_view import detect_communities  # noqa: E402
from term_db import (  # noqa: E402
    connect,
    get_aliases,
    jaccard,
    list_community_names,
    members_hash,
    read_community_name,
    read_edges,
    read_term_stats,
    read_verbs_for_signals,
    save_community_name,
    signals_containing_terms,
)

BATCH_DIR = Path("/tmp/note-topix-communities")
TOP_VERBS = 20
SAMPLE_SIGNALS = 5


def build_community_bundles(
    conn,
    noun_mode: str,
    method: str,
    min_occurrence: int,
    min_weight: float,
) -> list[dict]:
    term_stats = read_term_stats(conn, noun_mode)
    edges, _ = read_edges(conn, noun_mode, method)

    nodes = {t for t, meta in term_stats.items() if meta["occurrence"] >= min_occurrence}
    g = nx.Graph()
    for pair, w in edges.items():
        if w < min_weight:
            continue
        a, b = tuple(pair)
        if a in nodes and b in nodes:
            g.add_edge(a, b, weight=float(w))
    g.remove_nodes_from([n for n, d in g.degree() if d == 0])
    if g.number_of_nodes() == 0:
        return []
    node_to_com = detect_communities(g)

    # Group members by community id
    by_com: dict[int, list[str]] = {}
    for node, cid in node_to_com.items():
        by_com.setdefault(cid, []).append(node)

    bundles: list[dict] = []
    for cid, members in sorted(by_com.items()):
        if len(members) < 3:
            continue  # skip tiny communities
        # Rank members by occurrence
        members_ranked = sorted(
            members, key=lambda t: term_stats[t]["occurrence"], reverse=True
        )
        # Verbs from signals that contain any member (noun_mode="all" for signal_terms lookup)
        sig_ids = signals_containing_terms(conn, members_ranked, noun_mode)
        verb_counts = read_verbs_for_signals(conn, sig_ids)
        top_verbs = sorted(verb_counts.items(), key=lambda x: -x[1])[:TOP_VERBS]

        # Sample signal titles from a few of those signals
        sample_titles: list[str] = []
        if sig_ids:
            placeholders = ",".join("?" * len(sig_ids[:20]))
            rows = conn.execute(
                f"""
                SELECT source, title, engagement FROM signals
                 WHERE signal_id IN ({placeholders})
                 ORDER BY engagement DESC LIMIT ?
                """,
                [*sig_ids[:20], SAMPLE_SIGNALS],
            ).fetchall()
            sample_titles = [f"[{r[0]}] {r[1]}" for r in rows if r[1]]

        bundles.append(
            {
                "community_id": cid,
                "size": len(members),
                "members_hash": members_hash(members),
                "members": members_ranked,
                "top_verbs": top_verbs,
                "sample_titles": sample_titles,
            }
        )
    return bundles


def build_buffer_bundles(conn, view: dict, min_size: int = 3) -> list[dict]:
    """Community bundles for the buffer-overlap network under ``view``."""
    net = build_network(conn, BufferConfig(window=view["window"], include_verbs=view["include_verbs"]))
    nodes, edges, node_to_com = net.partition(view["min_occurrence"], view["min_weight"], view["max_nodes"])
    by_com: dict[int, list[str]] = {}
    for node, cid in node_to_com.items():
        by_com.setdefault(cid, []).append(node)
    aliases = get_aliases(conn)

    bundles: list[dict] = []
    for cid, members in sorted(by_com.items()):
        if len(members) < min_size:
            continue
        members_ranked = sorted(members, key=lambda t: -len(net.cores[t].signals))
        shared = net.community_buffers(members_ranked, top_k=30)
        observed = lambda f: sum(net.observed.get(m, {}).get(f, 0) for m in members_ranked)  # noqa: E731
        buffers = [f for f, _ in shared if not f.startswith(VERB_PREFIX)][:15]
        top_verbs = [(f[len(VERB_PREFIX):], observed(f)) for f, _ in shared if f.startswith(VERB_PREFIX)][:10]
        member_set = set(members)
        intra = sorted(
            (e for e in edges if e["source"] in member_set and e["target"] in member_set),
            key=lambda e: -e["weight"],
        )[:8]

        sig_ids = sorted(set().union(*(net.cores[m].signals for m in members_ranked)))
        sample_titles: list[str] = []
        if sig_ids:
            placeholders = ",".join("?" * len(sig_ids))
            rows = conn.execute(
                f"""
                SELECT source, title FROM signals
                 WHERE signal_id IN ({placeholders}) AND title != ''
                 ORDER BY engagement DESC LIMIT ?
                """,
                [*sig_ids, SAMPLE_SIGNALS + 3],
            ).fetchall()
            sample_titles = [f"[{r[0]}] {r[1]}" for r in rows]

        bundles.append({
            "community_id": cid,
            "size": len(members),
            "members_hash": members_hash(members),
            "members": members_ranked,
            "member_aliases": {m: sorted(a for a, c in aliases.items() if c == m) for m in members_ranked if m in aliases.values()},
            "shared_buffers": buffers,
            "top_verbs": top_verbs,
            "strongest_links": [
                f"{e['source']} — {e['target']} ({e['weight']:.2f}): {', '.join(e['shared'])}" for e in intra
            ],
            "sample_titles": sample_titles,
        })
    return bundles


NAMING_PROMPT = """以下は、note.com 記事のテーマ探索のために作った固有名詞ネットワークの
コミュニティです。固有名詞同士は「同じ文書に出るか」ではなく、「前後に出てくる名詞・
動詞 (バッファ) がどれだけ重なるか」でつながっています。つまり、同じコミュニティの
固有名詞は **似た文脈で語られている** ものです。

各コミュニティについて、members (固有名詞)、shared_buffers (メンバーを結びつける
共有名詞)、top_verbs (共有動詞)、strongest_links (特に強い組と共有語)、
sample_titles (代表的なシグナルの見出し) を示します。

各コミュニティに以下を付けてください。
- name: 日本語 20字以内。メンバーの羅列ではなく、共有バッファが示す「何について
  語られているか」という切り口を表す名前 (例:「オープンモデルの性能比較」
  「フロンティアAI企業の規制とリスク」)。
- description: 日本語 80〜150字。なぜこれらが同じ文脈で語られているのかを、共有
  バッファを根拠に説明する。

出力は JSON 配列のみ (前置き・コードフェンスなし):
[{{"community_id": <int>, "name": "...", "description": "..."}}, ...]

コミュニティ:
{communities_json}
"""


def cmd_run(args: argparse.Namespace) -> int:
    conn = connect()
    view = {
        **DEFAULT_VIEW,
        **{k: v for k, v in {
            "window": args.window, "min_occurrence": args.min_occurrence,
            "min_weight": args.min_weight, "max_nodes": args.max_nodes,
        }.items() if v is not None},
    }
    if args.no_verbs:
        view["include_verbs"] = False
    config = {"method": "buffer", **view}
    bundles = build_buffer_bundles(conn, view, min_size=args.min_size)
    named_before = [c for c in list_community_names(conn) if c["config"].get("method") == "buffer"]

    todo: list[dict] = []
    for b in bundles:
        if read_community_name(conn, b["members_hash"]):
            print(f"  #{b['community_id']} already named")
            continue
        best = max(named_before, key=lambda c: jaccard(c["members"], b["members"]), default=None)
        if best and jaccard(best["members"], b["members"]) >= args.reuse_threshold:
            save_community_name(
                conn, b["members_hash"], config, b["members"], b["top_verbs"],
                best["name"], best["description"], model=f"reused:{best['members_hash']}",
                buffers=b["shared_buffers"],
            )
            print(f"  #{b['community_id']} reused name 「{best['name']}」 (drifted community)")
            continue
        todo.append(b)

    print(f"communities: {len(bundles)}  to name: {len(todo)}")
    if not todo:
        return 0
    payload = [
        {k: b[k] for k in ("community_id", "members", "member_aliases", "shared_buffers",
                           "top_verbs", "strongest_links", "sample_titles")}
        for b in todo
    ]
    prompt = NAMING_PROMPT.format(communities_json=json.dumps(payload, ensure_ascii=False, indent=1))
    rows = call_claude_json(prompt, model=args.model)
    by_id = {b["community_id"]: b for b in todo}
    for row in rows:
        b = by_id.get(row.get("community_id"))
        if not b or not row.get("name"):
            continue
        save_community_name(
            conn, b["members_hash"], config, b["members"], b["top_verbs"],
            row["name"], row.get("description"), model=args.model or "claude-cli",
            buffers=b["shared_buffers"],
        )
        print(f"  #{b['community_id']} 「{row['name']}」 {b['members']}")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    conn = connect()
    if args.method == "buffer":
        view = {**DEFAULT_VIEW, "min_occurrence": args.min_occurrence, "min_weight": args.min_weight}
        bundles = build_buffer_bundles(conn, view)
    else:
        bundles = build_community_bundles(
            conn,
            noun_mode=args.noun_mode,
            method=args.method,
            min_occurrence=args.min_occurrence,
            min_weight=args.min_weight,
        )
    print(f"communities: {len(bundles)}")
    for b in bundles:
        print(
            f"  #{b['community_id']:2d}  size={b['size']:3d}  "
            f"members[:5]={b['members'][:5]}"
        )
    BATCH_DIR.mkdir(exist_ok=True)
    # Bundle everything into one file — subagent can handle several dozen communities easily.
    config = {
        "noun_mode": args.noun_mode,
        "method": args.method,
        "min_occurrence": args.min_occurrence,
        "min_weight": args.min_weight,
    }
    out = BATCH_DIR / "prompt_input.json"
    out.write_text(
        json.dumps({"config": config, "communities": bundles}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {out}")
    print(f"expected subagent output: {BATCH_DIR / 'prompt_output.json'}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    conn = connect()
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    config = data.get("config", {})
    imported = 0
    for row in data.get("names", []):
        try:
            save_community_name(
                conn,
                members_hash=row["members_hash"],
                config=config,
                members=row.get("members") or [],
                top_verbs=row.get("top_verbs") or [],
                name=row["name"],
                description=row.get("description"),
                model=args.model,
                buffers=row.get("shared_buffers"),
            )
            imported += 1
        except KeyError:
            continue
    print(f"imported {imported} community names")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prep = sub.add_parser("prepare", help="Compute communities and write subagent prompt")
    p_prep.add_argument("--noun-mode", default="all")
    p_prep.add_argument("--method", default="npmi")
    p_prep.add_argument("--min-occurrence", type=int, default=8)
    p_prep.add_argument("--min-weight", type=float, default=0.25)
    p_prep.set_defaults(func=cmd_prepare)

    p_run = sub.add_parser("run", help="Name buffer-network communities via `claude -p` (unattended)")
    p_run.add_argument("--window", type=int)
    p_run.add_argument("--no-verbs", action="store_true")
    p_run.add_argument("--min-occurrence", type=int)
    p_run.add_argument("--min-weight", type=float)
    p_run.add_argument("--max-nodes", type=int)
    p_run.add_argument("--min-size", type=int, default=3, help="skip smaller communities")
    p_run.add_argument("--reuse-threshold", type=float, default=0.6)
    p_run.add_argument("--model")
    p_run.set_defaults(func=cmd_run)

    p_imp = sub.add_parser("import", help="Import subagent-produced community names")
    p_imp.add_argument("file")
    p_imp.add_argument("--model", default="subagent")
    p_imp.set_defaults(func=cmd_import)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
