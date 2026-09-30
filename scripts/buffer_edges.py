#!/usr/bin/env python3
"""Buffer-overlap network over proper nouns.

Proper nouns rarely co-occur in the same signal, so direct co-occurrence
(NPMI etc.) leaves them mostly isolated. Instead, each proper noun (the
*core*) gets a profile of the topic-bearing common nouns — and optionally
verbs — found within ±N slots of its mentions (the *buffer*). Two cores are
linked by the cosine similarity of their buffer profiles: "talked about in
similar terms", which works even when they never appear together.

Design decisions:
  - Window: ±N noun slots (a slot is one noun run, compound included) and,
    if enabled, ±N verbs, never crossing a sentence/line boundary.
  - Co-mention signals are NOT excluded: appearing together is itself
    evidence of closeness, and it shows up as shared buffer naturally.
  - Genre normalization: each buffer feature is weighted by PPMI where the
    expected count comes from that feature's rate *within the mention's own
    source bucket*. Words that are merely typical of a genre (arXiv's
    "benchmark", news-list "結果") get discounted, so edges reflect content
    rather than which source two cores happen to live in.
  - Buffer vocabulary: common nouns (CONCEPT / GENERIC) judged topic-bearing
    in ``buffer_vocab`` (LLM, see scripts/buffer_prep.py). Before any
    judgments exist, CONCEPT / GENERIC minus BUFFER_STOPWORDS is used.
  - Cores are canonicalized via ``term_aliases`` ("hokusai", "katsushika"
    -> "katsushika hokusai").

Preview from the CLI:

    uv run scripts/buffer_edges.py --window 5 --top 40
    uv run scripts/buffer_edges.py --window 5 --no-verbs --profile claude
"""
from __future__ import annotations

import argparse
import bisect
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import networkx as nx  # noqa: E402
from network_view import detect_communities  # noqa: E402
from term_db import (  # noqa: E402
    PROPER_LABELS,
    connect,
    get_aliases,
    get_buffer_vocab,
    get_classifications,
    read_signal_slots,
)

BUFFER_LABELS = frozenset({"CONCEPT", "GENERIC"})

# Safety net for template / UI words that leak from source formats, applied
# even after LLM vocab judgments.
BUFFER_STOPWORDS = {
    "メニュー", "メニューニュース", "ニュース", "配信", "記事", "写真", "動画", "一覧",
    "excerpt", "note", "watch", "source", "link", "post", "thread", "abstract",
    "v:よる",  # 〜による — function verb that the verb stoplist misses
}

VERB_PREFIX = "v:"

# The Network page's defaults for method=buffer. Auto-naming / auto-briefing
# partition the graph with exactly these, so their community hashes match
# what the UI shows out of the box.
DEFAULT_VIEW = {
    "window": 5,
    "include_verbs": True,
    "min_occurrence": 2,
    "min_weight": 0.15,
    "max_nodes": 150,
}


@dataclass(frozen=True)
class BufferConfig:
    window: int = 5
    include_verbs: bool = True
    core_labels: frozenset[str] = frozenset(PROPER_LABELS)
    min_feature_df: int = 2      # a feature must appear in >= this many signals
    min_shared: int = 2          # an edge needs >= this many shared features
    cds_alpha: float = 0.75      # context-distribution smoothing for PPMI


@dataclass
class Core:
    term: str
    label: str
    aliases: set[str] = field(default_factory=set)
    signals: set[int] = field(default_factory=set)
    mentions: int = 0


@dataclass
class Corpus:
    """What the network needs from the DB, loaded once per pipeline run."""
    slots: dict[int, list[tuple[int, int, str, tuple[str, ...]]]]  # sid -> (off, sent, kind, units)
    bucket: dict[int, str]
    engagement: dict[int, float]

    @classmethod
    def load(cls, conn) -> "Corpus":
        bucket: dict[int, str] = {}
        engagement: dict[int, float] = {}
        for sid, b, e in conn.execute("SELECT signal_id, bucket, engagement FROM signals"):
            bucket[sid] = b or ""
            engagement[sid] = float(e or 0)
        return cls(read_signal_slots(conn), bucket, engagement)


class BufferNetwork:
    def __init__(
        self,
        corpus: Corpus,
        labels: dict[str, str],
        aliases: dict[str, str],
        vocab: dict[str, bool],
        config: BufferConfig,
    ) -> None:
        self.config = config
        self.corpus = corpus
        self.cores: dict[str, Core] = {}
        self.observed: dict[str, Counter] = {}
        self.vectors: dict[str, dict[str, float]] = {}
        self._norms: dict[str, float] = {}
        self._build(labels, aliases, vocab)

    # ---------- construction ----------

    def _core_of(self, unit: str, labels: dict[str, str], aliases: dict[str, str]) -> str | None:
        canonical = aliases.get(unit, unit)
        label = labels.get(canonical) or labels.get(unit)
        if label not in self.config.core_labels:
            return None
        core = self.cores.get(canonical)
        if core is None:
            core = self.cores[canonical] = Core(canonical, label)
        if unit != canonical:
            core.aliases.add(unit)
        return canonical

    def _build(self, labels: dict[str, str], aliases: dict[str, str], vocab: dict[str, bool]) -> None:
        cfg = self.config
        alias_keys = set(aliases)
        # A label chosen as core (e.g. CONCEPT) can't double as buffer.
        buffer_labels = BUFFER_LABELS - cfg.core_labels

        def noun_ok(u: str) -> bool:
            if u in BUFFER_STOPWORDS or u in alias_keys:
                return False
            if labels.get(u) not in buffer_labels:
                return False
            # Once any judgments exist, unjudged terms wait for `buffer_prep.py vocab run`.
            return vocab.get(u, False) if vocab else True

        # Pass 1: per-signal sequences, feature document frequency, bucket backgrounds.
        docs: dict[int, tuple[list, list, list[int]]] = {}
        feature_df: Counter = Counter()
        bg_slots: dict[str, Counter] = defaultdict(Counter)   # bucket -> {"n": slots, "v": slots}
        bg_counts: dict[tuple[str, str], Counter] = defaultdict(Counter)  # (bucket, kind) -> feature counts
        for sid, seq in self.corpus.slots.items():
            b = self.corpus.bucket.get(sid, "")
            nouns: list[tuple[int, int, tuple[str, ...], list[str]]] = []  # (seq_idx, sent, units, features)
            verbs: list[tuple[int, int, str]] = []                          # (seq_idx, sent, feature)
            seen: set[str] = set()
            last_noun: tuple[str, ...] = ()
            for i, (_off, sent, kind, units) in enumerate(seq):
                if kind == "n":
                    feats = [u for u in units if noun_ok(u)]
                    nouns.append((i, sent, units, feats))
                    last_noun = units
                    bg_slots[b]["n"] += 1
                    bg_counts[(b, "n")].update(set(feats))
                    seen.update(feats)
                elif cfg.include_verbs:
                    # サ変 nouns (開催, 走塁ミス's ミス) are emitted both inside a noun
                    # run and as a verb right after it; count them once, as the noun.
                    if units[0] in last_noun:
                        continue
                    f = VERB_PREFIX + units[0]
                    if f in BUFFER_STOPWORDS:
                        continue
                    verbs.append((i, sent, f))
                    bg_slots[b]["v"] += 1
                    bg_counts[(b, "v")][f] += 1
                    seen.add(f)
            feature_df.update(seen)
            docs[sid] = (nouns, verbs, [v[0] for v in verbs])

        keep = {f for f, n in feature_df.items() if n >= cfg.min_feature_df}

        # Smoothed per-bucket probability that one slot carries feature f.
        bg_prob: dict[tuple[str, str], dict[str, float]] = {}
        for (b, kind), counts in bg_counts.items():
            counts = {f: c for f, c in counts.items() if f in keep}
            if not counts:
                continue
            mass = sum(counts.values()) / max(1, bg_slots[b][kind])
            z = sum(c ** cfg.cds_alpha for c in counts.values())
            bg_prob[(b, kind)] = {f: mass * c ** cfg.cds_alpha / z for f, c in counts.items()}

        # Pass 2: mention windows.
        w = cfg.window
        observed: dict[str, Counter] = defaultdict(Counter)
        exposure: dict[str, Counter] = defaultdict(Counter)   # core -> {(bucket, kind): window slots}
        for sid, (nouns, verbs, verb_idx) in docs.items():
            b = self.corpus.bucket.get(sid, "")
            for j, (seq_i, sent, units, _) in enumerate(nouns):
                cores = {c for u in units if (c := self._core_of(u, labels, aliases))}
                if not cores:
                    continue
                window_feats: set[str] = set()
                n_slots = 0
                for k in range(max(0, j - w), min(len(nouns), j + w + 1)):
                    if k == j or nouns[k][1] != sent:
                        continue
                    n_slots += 1
                    window_feats.update(f for f in nouns[k][3] if f in keep)
                v_slots = 0
                if cfg.include_verbs:
                    pos = bisect.bisect_left(verb_idx, seq_i)
                    before = [v for v in verbs[max(0, pos - w):pos] if v[1] == sent]
                    after = [v for v in verbs[pos:pos + w] if v[1] == sent]
                    v_slots = len(before) + len(after)
                    window_feats.update(f for _, _, f in before + after if f in keep)
                for c in cores:
                    core = self.cores[c]
                    core.signals.add(sid)
                    core.mentions += 1
                    observed[c].update(window_feats)
                    exposure[c][(b, "n")] += n_slots
                    exposure[c][(b, "v")] += v_slots

        # PPMI vectors against the bucket-conditional expectation.
        for c, obs in observed.items():
            vec: dict[str, float] = {}
            for f, o in obs.items():
                kind = "v" if f.startswith(VERB_PREFIX) else "n"
                expected = sum(
                    slots * bg_prob.get((b, k), {}).get(f, 0.0)
                    for (b, k), slots in exposure[c].items()
                    if k == kind
                )
                if expected <= 0:
                    continue
                pmi = math.log(o / expected)
                if pmi > 0:
                    vec[f] = pmi
            self.observed[c] = obs
            self.vectors[c] = vec
            self._norms[c] = math.sqrt(sum(v * v for v in vec.values()))

    # ---------- queries ----------

    def similarity(self, a: str, b: str, top_k: int = 5) -> tuple[float, list[tuple[str, float]]]:
        """Cosine of the two PPMI profiles, plus the top shared features.

        Returns 0 when fewer than ``min_shared`` features overlap: a single
        shared word is usually coincidence, not a common angle.
        """
        va, vb = self.vectors.get(a, {}), self.vectors.get(b, {})
        na, nb = self._norms.get(a, 0.0), self._norms.get(b, 0.0)
        if not na or not nb:
            return 0.0, []
        if len(va) > len(vb):
            va, vb = vb, va
        contrib = [(f, x * vb[f] / (na * nb)) for f, x in va.items() if f in vb]
        if len(contrib) < self.config.min_shared:
            return 0.0, []
        contrib.sort(key=lambda p: -p[1])
        return sum(c for _, c in contrib), contrib[:top_k]

    def edges(self, nodes: list[str], min_weight: float, top_k: int = 5) -> list[dict]:
        out = []
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                s, shared = self.similarity(a, b, top_k)
                if s >= min_weight and s > 0:
                    out.append({
                        "source": a,
                        "target": b,
                        "weight": s,
                        "shared": [f for f, _ in shared],
                        "co_mentions": len(self.cores[a].signals & self.cores[b].signals),
                    })
        return out

    def profile(self, term: str, top_k: int = 20) -> list[dict]:
        vec = self.vectors.get(term, {})
        obs = self.observed.get(term, Counter())
        ranked = sorted(vec.items(), key=lambda p: -p[1])[:top_k]
        return [{"feature": f, "ppmi": v, "count": obs[f]} for f, v in ranked]

    def neighbors(self, term: str, top_k: int = 15, shared_k: int = 6) -> list[dict]:
        out = []
        for other in self.vectors:
            if other == term:
                continue
            s, shared = self.similarity(term, other, shared_k)
            if s > 0:
                out.append({
                    "term": other,
                    "weight": s,
                    "shared": [f for f, _ in shared],
                    "co_mentions": len(self.cores[term].signals & self.cores[other].signals),
                })
        out.sort(key=lambda d: -d["weight"])
        return out[:top_k]

    def community_buffers(self, members: list[str], top_k: int = 15) -> list[tuple[str, float]]:
        """Features that tie a community together: summed PPMI over members,
        restricted to features at least two members share."""
        total: Counter = Counter()
        holders: Counter = Counter()
        for m in members:
            for f, v in self.vectors.get(m, {}).items():
                total[f] += v
                holders[f] += 1
        shared = [(f, v) for f, v in total.items() if holders[f] >= 2]
        shared.sort(key=lambda p: -p[1])
        return shared[:top_k]

    def partition(
        self, min_occurrence: int, min_weight: float, max_nodes: int, community: bool = True,
    ) -> tuple[list[str], list[dict], dict[str, int]]:
        """(connected nodes, edges, Louvain node->community) for a view config."""
        occ = {t: len(c.signals) for t, c in self.cores.items()}
        nodes = sorted((t for t in occ if occ[t] >= min_occurrence), key=lambda t: (-occ[t], t))[:max_nodes]
        edges = self.edges(nodes, min_weight)
        connected = {e["source"] for e in edges} | {e["target"] for e in edges}
        nodes = [t for t in nodes if t in connected]
        node_to_com: dict[str, int] = {}
        if community and edges:
            g = nx.Graph()
            for e in edges:
                g.add_edge(e["source"], e["target"], weight=e["weight"])
            node_to_com = detect_communities(g)
        return nodes, edges, node_to_com

    def core_stats(self, term: str) -> dict:
        core = self.cores[term]
        buckets = Counter(self.corpus.bucket.get(s, "") for s in core.signals)
        return {
            "occurrence": len(core.signals),
            "mentions": core.mentions,
            "engagement_sum": sum(self.corpus.engagement.get(s, 0.0) for s in core.signals),
            "buckets": sorted(buckets),
            "dominant_bucket": buckets.most_common(1)[0][0] if buckets else "",
            "aliases": sorted(core.aliases),
            "label": core.label,
        }


def build_network(conn, config: BufferConfig, corpus: Corpus | None = None) -> BufferNetwork:
    labels = {t: v["label"] for t, v in get_classifications(conn).items()}
    return BufferNetwork(
        corpus or Corpus.load(conn),
        labels,
        get_aliases(conn),
        get_buffer_vocab(conn),
        config,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window", type=int, default=5)
    parser.add_argument("--no-verbs", action="store_true")
    parser.add_argument("--min-occurrence", type=int, default=2, help="min signals mentioning a core")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--profile", help="show one core's buffer profile + neighbors")
    args = parser.parse_args()

    conn = connect()
    net = build_network(conn, BufferConfig(window=args.window, include_verbs=not args.no_verbs))
    if args.profile:
        print(f"== {args.profile}  {net.core_stats(args.profile)}")
        for p in net.profile(args.profile):
            print(f"  {p['ppmi']:.2f}  x{p['count']}  {p['feature']}")
        print("-- neighbors")
        for n in net.neighbors(args.profile):
            print(f"  {n['weight']:.2f} co={n['co_mentions']} {n['term']} :: {', '.join(n['shared'])}")
        return 0
    nodes = [t for t in net.cores if len(net.cores[t].signals) >= args.min_occurrence]
    edges = sorted(net.edges(nodes, 0.0), key=lambda e: -e["weight"])
    dom = {t: net.core_stats(t)["dominant_bucket"] for t in nodes}
    cross = [e for e in edges if dom[e["source"]] != dom[e["target"]]]
    print(f"cores={len(net.cores)} nodes(min_occ {args.min_occurrence})={len(nodes)} edges>0={len(edges)}")
    for label, rows in (("top", edges), ("cross-bucket top", cross)):
        print(f"\n== {label}")
        for e in rows[:args.top]:
            a, b = e["source"], e["target"]
            print(f"  {e['weight']:.2f} co={e['co_mentions']} {a}[{dom[a]}] — {b}[{dom[b]}] :: {', '.join(e['shared'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
