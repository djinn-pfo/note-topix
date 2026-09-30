"""Pyvis rendering helpers for co-occurrence networks.

Kept out of analyze_signals.py so the analyzer stays importable without
the heavier pyvis/networkx stack when only the CLI is needed.
"""
from __future__ import annotations

from collections import Counter

import networkx as nx
from networkx.algorithms.community import louvain_communities
from pyvis.network import Network

# Bucket -> node color (Streamlit-friendly palette)
BUCKET_COLORS = {
    "academic": "#7986cb",   # indigo
    "hn": "#ff8a65",         # orange
    "jp-tech": "#4db6ac",    # teal
    "jp-social": "#f06292",  # pink
    "general-jp": "#fdd835", # yellow
    "curated": "#9575cd",    # purple
}
DEFAULT_COLOR = "#90a4ae"

# Distinct-hue palette for Louvain communities; cycled if there are more
# communities than colors. Chosen to be visually distinguishable at a glance.
COMMUNITY_COLORS = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
    "#dcbeff", "#9a6324", "#800000", "#aaffc3", "#808000",
    "#ffd8b1", "#000075", "#a9a9a9",
]


def dominant_bucket(term: str, index) -> str:
    """Bucket that contributes the most signals for this term."""
    stats = index.get(term)
    if not stats:
        return ""
    counts = Counter(s.bucket for s in stats.signals)
    return counts.most_common(1)[0][0] if counts else ""


def build_nx_graph(
    nodes: list[str],
    edges: dict,  # dict[frozenset[str], float]
    index,
    min_weight: float = 0.0,
    weight_label: str = "weight",
    edge_scale: float = 4.0,
) -> nx.Graph:
    """Build a networkx graph. Callers apply their own occurrence/weight filters
    before choosing the ``nodes`` list."""
    node_set = set(nodes)
    g = nx.Graph()
    for term in nodes:
        stats = index.get(term)
        engagement = sum(s.engagement for s in stats.signals) if stats else 0.0
        bucket = dominant_bucket(term, index)
        occurrence = stats.count if stats else 0
        g.add_node(
            term,
            size=8 + min(30, engagement / 200),
            color=BUCKET_COLORS.get(bucket, DEFAULT_COLOR),
            title=(
                f"{term}<br>"
                f"occurrence: {occurrence}<br>"
                f"buckets: {len(stats.buckets) if stats else '-'} "
                f"({', '.join(sorted(stats.buckets)) if stats else '-'})<br>"
                f"engagement: {int(engagement)}<br>"
                f"dominant: {bucket or '-'}"
            ),
            label=term,
        )
    for pair, w in edges.items():
        if w < min_weight:
            continue
        a, b = tuple(pair)
        if a in node_set and b in node_set:
            g.add_edge(
                a,
                b,
                weight=float(w),
                width=0.5 + edge_scale * max(0.0, float(w)),
                title=f"{weight_label} {w:.3f}",
            )
    return g


def detect_communities(g: nx.Graph, resolution: float = 1.0) -> dict[str, int]:
    """Run Louvain on ``g`` (using edge ``weight``). Returns node -> community id."""
    if g.number_of_edges() == 0:
        return {node: 0 for node in g.nodes()}
    communities = louvain_communities(g, weight="weight", resolution=resolution, seed=42)
    node_to_com: dict[str, int] = {}
    for idx, members in enumerate(communities):
        for node in members:
            node_to_com[node] = idx
    return node_to_com


def apply_community_colors(g: nx.Graph, node_to_com: dict[str, int]) -> None:
    """Overwrite node colors so members of the same community share a hue."""
    for node in g.nodes():
        com_id = node_to_com.get(node, 0)
        color = COMMUNITY_COLORS[com_id % len(COMMUNITY_COLORS)]
        g.nodes[node]["color"] = color
        title = g.nodes[node].get("title", node)
        g.nodes[node]["title"] = f"community: {com_id}<br>{title}"


def render_pyvis_html(g: nx.Graph, height_px: int = 550) -> str:
    net = Network(
        height=f"{height_px}px",
        width="100%",
        bgcolor="#ffffff",
        font_color="#222",
        directed=False,
        notebook=False,
        cdn_resources="in_line",
    )
    # Physics tuned for readable layouts on ~15-100 node graphs.
    net.barnes_hut(
        gravity=-2500,
        central_gravity=0.2,
        spring_length=120,
        spring_strength=0.02,
        damping=0.35,
    )
    net.from_nx(g)
    # Disable interactive physics after initial layout for stability.
    net.set_options(
        """
        {
          "physics": {"stabilization": {"iterations": 200}, "enabled": true},
          "interaction": {"hover": true, "tooltipDelay": 100}
        }
        """
    )
    return net.generate_html(notebook=False)
