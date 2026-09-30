"""Streamlit dashboard for the note-topix correlation pipeline.

**Read-only view over the SQLite pipeline output.** All heavy work (tokenizing,
edge computation, note.com coverage lookups) is performed by
``scripts/run_pipeline.py`` on a cron schedule; this dashboard just displays
what was persisted.

Run bound to your Tailscale IP so only tailnet members can reach it:

    uv run streamlit run scripts/dashboard.py \\
        --server.address $(tailscale ip -4 | head -1) \\
        --server.port 8501

Use the "🔄 再読込み" button in the sidebar to bust the read cache after a
pipeline run.
"""
from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import streamlit as st

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from analyze_signals import (  # noqa: E402
    SOURCE_BUCKETS,
    differentiation_hint,
    neighborhood,
)
from network_view import (  # noqa: E402
    apply_community_colors,
    build_nx_graph,
    detect_communities,
    render_pyvis_html,
)
from term_db import (  # noqa: E402
    LABELS,
    PROPER_LABELS,
    connect as db_connect,
    get_classifications,
    get_meta,
    list_community_names,
    members_hash,
    read_all_note_coverage,
    read_community_name,
    read_edges,
    read_signals,
    read_signals_for_term,
    read_term_stats,
)
import streamlit.components.v1 as components  # noqa: E402


# Lightweight replacements for analyze_signals dataclasses; the dashboard only
# needs attribute access, not the tokenizer plumbing.
@dataclass
class SignalRow:
    path: Path
    bucket: str
    source: str
    title: str
    body: str
    engagement: float
    date: str
    url: str = ""


@dataclass
class TermStatsProxy:
    count: int
    buckets: set
    engagement_sum: float
    signals: list  # populated lazily by callers if needed


def _row_to_signal(row: dict) -> SignalRow:
    return SignalRow(
        path=Path(row["path"]),
        bucket=row.get("bucket") or "",
        source=row.get("source") or "",
        title=row.get("title") or "",
        body=row.get("body") or "",
        engagement=float(row.get("engagement") or 0.0),
        date=row.get("date") or "",
        url=row.get("url") or "",
    )

st.set_page_config(
    page_title="note-topix research",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(show_spinner="DBから読込中...")
def cached_load_from_db(_cache_key: int, noun_mode: str):
    """Return the same shape as before: (signals, index, ranked_all, meta)."""
    conn = db_connect()
    try:
        signal_rows = read_signals(conn)
        signals = [_row_to_signal(r) for r in signal_rows]

        term_stats = read_term_stats(conn, noun_mode)
        index: dict[str, TermStatsProxy] = {}
        for term, meta in term_stats.items():
            index[term] = TermStatsProxy(
                count=meta["occurrence"],
                buckets=meta["buckets"],
                engagement_sum=meta["engagement_sum"],
                signals=[],  # populated on demand for the deep-dive tab
            )

        # Ranking matches analyze_signals.rank_terms: (buckets, engagement_sum, count)
        candidates = [
            (t, s) for t, s in index.items() if len(s.buckets) >= 2
        ]
        candidates.sort(
            key=lambda pair: (
                len(pair[1].buckets),
                pair[1].engagement_sum,
                pair[1].count,
            ),
            reverse=True,
        )

        meta = {
            "last_run_at": get_meta(conn, "last_run_at"),
            "signal_count": get_meta(conn, "signal_count"),
        }
        return signals, index, candidates, meta
    finally:
        conn.close()


@st.cache_data(show_spinner="エッジ読込中...")
def cached_edges(_cache_key: int, noun_mode: str, method: str):
    conn = db_connect()
    try:
        return read_edges(conn, noun_mode, method)
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def cached_all_note_coverage(_cache_key: int) -> dict[str, dict]:
    conn = db_connect()
    try:
        return read_all_note_coverage(conn)
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def cached_signals_for_term(_cache_key: int, term: str, noun_mode: str) -> list:
    conn = db_connect()
    try:
        rows = read_signals_for_term(conn, term, noun_mode)
    finally:
        conn.close()
    return [_row_to_signal(r) for r in rows]


@st.cache_data(show_spinner=False)
def cached_classifications(_cache_key: int) -> dict[str, dict]:
    conn = db_connect()
    try:
        return get_classifications(conn)
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def cached_saved_communities(_cache_key: int) -> list[dict]:
    conn = db_connect()
    try:
        return list_community_names(conn)
    finally:
        conn.close()


def term_time_series(term_signals: list) -> pd.DataFrame:
    """One row per (date, bucket) with signal count."""
    counter: Counter = Counter()
    for signal in term_signals:
        if not signal.date:
            continue
        counter[(signal.date, signal.bucket)] += 1
    if not counter:
        return pd.DataFrame(columns=["date", "bucket", "count"])
    rows = [{"date": d, "bucket": b, "count": c} for (d, b), c in counter.items()]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date")


def bucket_summary(signals) -> pd.DataFrame:
    counts = Counter(s.bucket for s in signals)
    return pd.DataFrame(
        [{"bucket": b, "signals": n} for b, n in sorted(counts.items())]
    )


def coverage_badge(total: int | None) -> str:
    if total is None:
        return "?"
    if total == 0:
        return "🟢 0 未開拓"
    if total < 10:
        return f"🟢 {total} 希薄"
    if total < 100:
        return f"🟡 {total}"
    if total < 1000:
        return f"🟠 {total}"
    return f"🔴 {total} 飽和"


# ---------- Sidebar ----------

st.sidebar.title("note-topix")
st.sidebar.caption("横断相関 + note.com飽和度")

if "reload_key" not in st.session_state:
    st.session_state.reload_key = 0
if st.sidebar.button("🔄 シグナル再読込み", use_container_width=True):
    st.session_state.reload_key += 1
    st.cache_data.clear()

noun_mode = st.sidebar.radio(
    "ターム抽出モード",
    ["all", "propn", "ai"],
    format_func={
        "all": "全名詞 (POS 普通+固有)",
        "propn": "固有名詞のみ (POS)",
        "ai": "AI分類 (DB ラベル)",
    }.get,
    help=(
        "all/propnはspaCy+SudachiのPOS判定。"
        "aiはterm DBに保存されたClaude subagentのラベルで絞り込む。"
    ),
)

ai_label_filter: set[str] = set()
if noun_mode == "ai":
    with st.sidebar.expander("ℹ️ ラベルの意味", expanded=False):
        st.markdown(
            "- `PROPER_PRODUCT`: 製品/モデル/フレームワーク名 (Claude, GPT-5, PyTorch)\n"
            "- `PROPER_ORG`: 企業/研究所/団体 (Anthropic, OpenAI, 松尾研)\n"
            "- `PROPER_PERSON`: 人名\n"
            "- `PROPER_PLACE`: 地名 (Tokyo, 東京, Japan)\n"
            "- `PROPER_WORK`: 論文/規格/イベント名 (MCP, ICML 2026)\n"
            "- `CONCEPT`: 技術概念/手法 (prompt injection, 強化学習)。"
            "**単語一語だけの技術用語（training, learning, latency 等）も含まれる**ため、"
            "純粋な固有名詞探しでは外すのが吉。\n"
            "- `GENERIC`: 一般名詞 (framework, performance)\n"
            "- `JUNK`: トークン化アーティファクト"
        )
    ai_label_filter = set(
        st.sidebar.multiselect(
            "採用ラベル (これに該当するタームだけを描画)",
            sorted(LABELS),
            default=sorted(PROPER_LABELS),
            help=(
                "選択したラベルの分類を持つタームだけを描画する。未分類は含まれない。"
                "CONCEPTは技術用語一般が入るので、真の固有名詞だけ見たい場合は外す。"
            ),
        )
    )
top_n = st.sidebar.slider("表示上位件数", 10, 100, 30, step=10)
min_buckets = st.sidebar.slider("最小バケット数", 2, len(set(SOURCE_BUCKETS.values())), 2)
show_note_coverage = st.sidebar.checkbox("note.com飽和度を取得", value=True)

# ---- Saved communities browser (independent of current graph config) ----
_saved_communities = cached_saved_communities(st.session_state.reload_key)
with st.sidebar.expander(
    f"🎨 保存済みコミュニティ ({len(_saved_communities)})",
    expanded=False,
):
    if not _saved_communities:
        st.caption(
            "まだ命名されたコミュニティがありません。\n"
            "`uv run scripts/name_communities.py prepare` で作れます。"
        )
    else:
        st.caption(
            "subagentが命名したコミュニティ一覧。"
            "配下のconfigでダッシュボードを設定すると"
            "コミュニティタブに名前が表示されます。"
        )
        for com in _saved_communities:
            cfg = com.get("config") or {}
            cfg_str = (
                f"mode={cfg.get('noun_mode','?')}, method={cfg.get('method','?')}, "
                f"min_occ={cfg.get('min_occurrence','?')}, min_w={cfg.get('min_weight','?')}"
            )
            with st.container(border=True):
                st.markdown(f"**{com['name']}**")
                if com.get("description"):
                    st.caption(com["description"])
                members = com.get("members") or []
                if members:
                    st.caption(f"メンバー ({len(members)}): {', '.join(members[:10])}"
                               + (" …" if len(members) > 10 else ""))
                verbs = com.get("top_verbs") or []
                if verbs:
                    st.caption("頻出動詞: " + ", ".join(f"{v}({c})" for v, c in verbs[:8]))
                st.caption(f"config: {cfg_str}")


# ---------- Data ----------

# AI mode: extract with "all" POS filter, then post-filter to labeled terms.
_extract_mode = "all" if noun_mode == "ai" else noun_mode
signals, index, ranked_all, pipeline_meta = cached_load_from_db(
    st.session_state.reload_key, noun_mode=_extract_mode
)

allowed_terms: set[str] | None = None
classifications: dict[str, dict] = {}
if noun_mode == "ai":
    classifications = cached_classifications(st.session_state.reload_key)
    allowed_terms = {
        t for t, meta in classifications.items() if meta["label"] in ai_label_filter
    }
    index = {t: s for t, s in index.items() if t in allowed_terms}
    ranked_all = [(t, s) for t, s in ranked_all if t in allowed_terms]
    st.sidebar.caption(
        f"DB分類済 {len(classifications):,} · "
        f"フィルタ通過 {len(allowed_terms):,} / index適用後 {len(index):,}"
    )
ranked = [
    (term, stats) for term, stats in ranked_all if len(stats.buckets) >= min_buckets
][:top_n]


# ---------- Header ----------

st.title("📊 note-topix リサーチダッシュボード")
mode_label = {"all": "全名詞", "propn": "固有名詞のみ", "ai": f"AI分類 ({', '.join(sorted(ai_label_filter))})"}[noun_mode]
last_run = pipeline_meta.get("last_run_at") or "(未実行)"
st.caption(
    f"抽出モード: **{mode_label}** ・ "
    f"シグナル {len(signals):,} 件 / ターム {len(index):,} / バケット横断 {len(ranked_all):,} ・ "
    f"最終計算: {last_run}"
)

bucket_df = bucket_summary(signals)
st.dataframe(
    bucket_df.set_index("bucket").T,
    use_container_width=True,
    hide_index=True,
)


# ---------- Coverage lookup (pre-fetched by run_pipeline.py) ----------

coverage_data: dict[str, dict] = {}
if show_note_coverage:
    coverage_data = cached_all_note_coverage(st.session_state.reload_key)


# ---------- Briefings viewer (top expander) ----------

BRIEFINGS_DIR = SCRIPTS_DIR.parent / "documents" / "briefings"


def list_briefings() -> list[Path]:
    if not BRIEFINGS_DIR.exists():
        return []
    files = sorted(BRIEFINGS_DIR.glob("*.md"), reverse=True)
    return files


def copy_button(text: str, label: str = "📋 クリップボードにコピー", key: str = "copy") -> None:
    """Render a prominent copy-to-clipboard button.

    Uses the Clipboard API where available (HTTPS / localhost / Tailscale),
    falls back to ``document.execCommand('copy')`` on older/insecure contexts.
    """
    import json as _json

    payload = _json.dumps(text)  # safe JS-string literal
    btn_id = f"copybtn_{key}"
    ta_id = f"copyta_{key}"
    html = f"""
    <textarea id="{ta_id}" style="position:absolute; left:-9999px;">{text.replace("</", "<\\/")}</textarea>
    <button id="{btn_id}" onclick="
        const txt = {payload};
        const done = () => {{
            const b = document.getElementById('{btn_id}');
            b.innerText = '✅ コピー済み';
            b.style.background = '#4CAF50';
            setTimeout(() => {{ b.innerText = '{label}'; b.style.background = '#0068c9'; }}, 2000);
        }};
        if (navigator.clipboard && navigator.clipboard.writeText) {{
            navigator.clipboard.writeText(txt).then(done).catch(() => {{
                const ta = document.getElementById('{ta_id}');
                ta.select(); document.execCommand('copy'); done();
            }});
        }} else {{
            const ta = document.getElementById('{ta_id}');
            ta.select(); document.execCommand('copy'); done();
        }}
    " style="
        padding: 10px 24px;
        font-size: 15px;
        font-weight: 600;
        color: white;
        background: #0068c9;
        border: none;
        border-radius: 6px;
        cursor: pointer;
        box-shadow: 0 2px 4px rgba(0,0,0,0.15);
    ">{label}</button>
    """
    components.html(html, height=60)


briefings = list_briefings()
with st.expander(
    f"📝 ブリーフィング ({len(briefings)}件) — note下書きの叩き台",
    expanded=False,
):
    if not briefings:
        st.info(
            "まだブリーフィングがありません。以下で生成できます:\n\n"
            "```\n"
            "uv run scripts/generate_briefing.py suggest --top 10\n"
            "uv run scripts/generate_briefing.py prepare --community \"<name>\"\n"
            "```\n"
            "その後 subagent がプロンプトを処理して "
            "`documents/briefings/YYYY-MM-DD-<slug>.md` を生成します。"
        )
    else:
        chosen = st.selectbox(
            "ブリーフィング選択",
            [p.name for p in briefings],
            key="briefing_pick",
        )
        chosen_path = BRIEFINGS_DIR / chosen
        content = chosen_path.read_text(encoding="utf-8")
        view_col, raw_col = st.tabs(["🖼️ プレビュー", "📋 コピー用 raw markdown"])
        with view_col:
            st.markdown(content)
        with raw_col:
            st.caption(
                "下のボタンでクリップボードにコピー → note の下書きに貼り付け → "
                "自分の視点で書き足していく。"
            )
            copy_button(content, key=f"brief_{chosen}")
            with st.expander("raw markdown (確認用)", expanded=False):
                st.code(content, language="markdown")
        st.caption(f"ファイル: `documents/briefings/{chosen}`")


st.divider()


# ---------- Cooccurrence Network Analyser ----------

st.header("Cooccurrence Network Analyser")
st.caption(
    "全ソースのタームから共起ネットワークを構築。"
    "エッジ重み計算方式・出現数フィルタ・コミュニティ検出を切り替えて"
    "関連ワード群を探索し、下部のノード選択で原典を確認する。"
)

# ---- Controls ----
c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
with c1:
    edge_method = st.radio(
        "エッジ重み方式",
        ["npmi", "pmi", "jaccard", "count"],
        format_func={
            "npmi": "NPMI (推奨・[-1,1]正規化)",
            "pmi": "PMI (対数比)",
            "jaccard": "Jaccard (集合類似)",
            "count": "生共起カウント",
        }.get,
        horizontal=False,
    )
with c2:
    min_occurrence = st.slider(
        "ノード最小出現数",
        1, 50, 8,
        help="このシグナル数未満しか出ない語は除外。1にすれば低頻度の固有名詞まで拾う。",
    )
with c3:
    # Edge weight thresholds vary widely by method; give a sane default per method.
    weight_range = {
        "npmi":    (-0.2, 1.0, 0.05, 0.25),
        "pmi":     (0.0, 8.0, 0.5, 3.0),
        "jaccard": (0.0, 1.0, 0.02, 0.10),
        "count":   (2.0, 50.0, 1.0, 4.0),
    }[edge_method]
    min_weight = st.slider(
        f"最小エッジ重み ({edge_method})",
        weight_range[0], weight_range[1], weight_range[3], step=weight_range[2],
    )
    max_nodes = st.slider("ノード数上限", 30, 400, 150, step=10, help="出現数上位から採用")
with c4:
    enable_community = st.checkbox("Louvainクラスタリング", value=True)
    color_by = st.radio(
        "ノード色",
        ["community", "bucket"],
        disabled=not enable_community,
        horizontal=True,
    )
    graph_height = st.slider("表示高さ(px)", 400, 1200, 720, step=40)

# ---- Compute edges + build graph ----
all_edges, pair_counts = cached_edges(
    st.session_state.reload_key, _extract_mode, edge_method
)
# Derive per-term occurrence counts from the term_stats index we already loaded.
term_counts = {t: s.count for t, s in index.items()}
if noun_mode == "ai" and allowed_terms is not None:
    # Restrict edges to pairs fully inside the allowed set, and prune counts.
    all_edges = {pair: w for pair, w in all_edges.items() if pair <= allowed_terms}
    term_counts = {t: c for t, c in term_counts.items() if t in allowed_terms}

# Node selection: filter by occurrence, then cap by top-N occurrence.
candidate_nodes = [t for t, cnt in term_counts.items() if cnt >= min_occurrence]
candidate_nodes.sort(key=lambda t: term_counts[t], reverse=True)
graph_nodes = candidate_nodes[:max_nodes]

if not graph_nodes:
    st.info("該当ノードなし。出現数条件を緩めてください。")
    st.stop()

g = build_nx_graph(
    graph_nodes,
    all_edges,
    index,
    min_weight=min_weight,
    weight_label=edge_method.upper(),
    edge_scale={"npmi": 4.0, "pmi": 0.5, "jaccard": 6.0, "count": 0.1}[edge_method],
)

# Drop isolated nodes (no edges under this threshold) so the layout stays tight.
isolated = [n for n, d in g.degree() if d == 0]
g.remove_nodes_from(isolated)

if g.number_of_nodes() == 0:
    st.info(
        "この閾値では描画できるエッジがありません。"
        "最小エッジ重みを下げるか、最小共起カウントを下げてください。"
    )
    st.stop()

# Communities
node_to_com: dict[str, int] = {}
if enable_community:
    node_to_com = detect_communities(g)
    if color_by == "community":
        apply_community_colors(g, node_to_com)

# Render
c_left, c_right = st.columns([3, 1])
with c_left:
    html = render_pyvis_html(g, height_px=graph_height)
    components.html(html, height=graph_height + 20, scrolling=False)
    st.caption(
        f"表示: {g.number_of_nodes()} ノード / {g.number_of_edges()} エッジ · "
        f"除外: 出現<{min_occurrence} または 孤立ノード{len(isolated)}件 · "
        f"コミュニティ: {len(set(node_to_com.values())) if node_to_com else '-'}"
    )
with c_right:
    st.markdown("**凡例**")
    if enable_community and color_by == "community":
        st.caption("色 = Louvainコミュニティ")
        # Community stats
        com_sizes = Counter(node_to_com.values())
        for com_id, size in com_sizes.most_common(10):
            st.write(f"- Com {com_id}: {size} nodes")
    else:
        st.caption("色 = 主要ソース")
        for bucket in sorted(set(SOURCE_BUCKETS.values())):
            st.write(f"- {bucket}")

# ---- Node inspection ----
st.subheader("選択ノードの詳細")

selected = st.selectbox(
    "ノードを選択（グラフ上のラベルと同じ）",
    sorted(g.nodes()),
    key="node_pick",
)

if selected:
    stats = index[selected]
    # Lazy-load signals for the selected term only (avoids loading all in cache).
    selected_signals = cached_signals_for_term(
        st.session_state.reload_key, selected, _extract_mode
    )
    top_col1, top_col2, top_col3, top_col4 = st.columns(4)
    top_col1.metric("出現数", term_counts.get(selected, 0))
    top_col2.metric("エンゲージメント計", f"{stats.engagement_sum:,.0f}")
    top_col3.metric("バケット数", len(stats.buckets))
    if enable_community:
        top_col4.metric("Community", node_to_com.get(selected, "-"))

    tab_signals, tab_neighbors, tab_community, tab_timeseries, tab_note = st.tabs(
        ["📰 原典シグナル", "🔗 隣接ターム", "🎨 コミュニティ", "📈 時系列", "💴 note.com"]
    )

    source_badges = {
        "hackernews": "🟧 HN",
        "arxiv": "📄 arXiv",
        "zenn": "📗 Zenn",
        "qiita": "📘 Qiita",
        "bluesky": "🦋 Bluesky",
        "google_trends": "🔎 GoogleTrends",
        "raindrop": "💧 Raindrop(X)",
    }

    with tab_signals:
        available_sources = sorted({s.source for s in selected_signals})
        selected_sources = st.multiselect(
            "ソース絞り込み",
            available_sources,
            default=available_sources,
            format_func=lambda s: source_badges.get(s, s),
        )
        sort_by = st.radio("並び順", ["エンゲージメント順", "新しい順"], horizontal=True)
        show_n = st.slider("表示件数", 3, 30, 10, key=f"show_n_{selected}")
        filtered = [s for s in selected_signals if s.source in selected_sources]
        if sort_by == "新しい順":
            filtered.sort(key=lambda s: s.date, reverse=True)
        else:
            filtered.sort(key=lambda s: s.engagement, reverse=True)
        if not filtered:
            st.info("該当シグナルなし。")
        for sig in filtered[:show_n]:
            badge = source_badges.get(sig.source, sig.source)
            title = sig.title.strip() or "(no title)"
            title_md = f"[{title}]({sig.url})" if sig.url else title
            header = f"{badge} · {sig.date or '-'} · eng={sig.engagement:g} — {title[:70]}"
            with st.expander(header, expanded=False):
                st.markdown(f"### {title_md}")
                if sig.url:
                    st.caption(sig.url)
                body_excerpt = sig.body.strip()
                if body_excerpt:
                    shown = body_excerpt[:1200]
                    if len(body_excerpt) > 1200:
                        shown += "\n\n…"
                    st.markdown(shown)
                else:
                    st.caption("(本文なし — タイトルのみ)")
                rel = sig.path.relative_to(SCRIPTS_DIR.parent)
                st.caption(f"📁 `{rel}`")

    with tab_neighbors:
        nbhd = neighborhood(selected, all_edges, k=30, min_weight=min_weight)
        if not nbhd:
            st.info("隣接ターム無し。エッジ閾値を下げてみてください。")
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "隣接ターム": t,
                            edge_method.upper(): round(w, 3),
                            "occurrence": term_counts.get(t, 0),
                            "in_graph": t in g.nodes(),
                        }
                        for t, w in nbhd
                    ]
                ),
                use_container_width=True,
                hide_index=True,
                height=420,
            )

    with tab_community:
        if not enable_community:
            st.info("Louvainクラスタリングが無効です。上部で有効化してください。")
        else:
            my_com = node_to_com.get(selected)
            if my_com is None:
                st.info("このノードにはコミュニティ情報がありません。")
            else:
                members = sorted(
                    [n for n, c in node_to_com.items() if c == my_com],
                    key=lambda n: term_counts.get(n, 0),
                    reverse=True,
                )
                # Look up subagent-provided name (if the current partition matches a cached hash).
                conn_named = db_connect()
                try:
                    name_row = read_community_name(conn_named, members_hash(members))
                finally:
                    conn_named.close()

                if name_row:
                    st.markdown(f"### 🎨 {name_row['name']}")
                    if name_row.get("description"):
                        st.markdown(name_row["description"])
                    top_verbs = name_row.get("top_verbs") or []
                    if top_verbs:
                        verb_summary = " · ".join(f"{v} ({c})" for v, c in top_verbs[:10])
                        st.caption(f"頻出動詞: {verb_summary}")
                else:
                    st.markdown(f"### Community {my_com}")
                    st.caption(
                        f"（未命名 — `uv run scripts/name_communities.py prepare` で "
                        f"バンドルを作成 → subagent命名 → `... import` で登録できます）"
                    )

                st.markdown(f"**{len(members)} メンバー:**")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "term": n,
                                "occurrence": term_counts.get(n, 0),
                                "buckets": len(index[n].buckets) if n in index else 0,
                            }
                            for n in members
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                    height=380,
                )

    with tab_timeseries:
        ts_df = term_time_series(selected_signals)
        if ts_df.empty:
            st.info("日付情報のあるシグナルがありません。")
        else:
            pivot = ts_df.pivot_table(
                index="date", columns="bucket", values="count", fill_value=0, aggfunc="sum",
            ).sort_index()
            st.line_chart(pivot, height=380)
            st.caption(
                f"データ期間: {pivot.index.min().date()} 〜 {pivot.index.max().date()}"
            )

    with tab_note:
        cov = coverage_data.get(selected)
        if cov is None or cov.get("total") is None:
            st.warning("取得に失敗しました。")
        else:
            total = cov.get("total")
            col_m1, col_m2 = st.columns(2)
            col_m1.metric("該当有料記事数", f"{total:,}", help="10000は上限")
            col_m2.write(coverage_badge(total))
            st.markdown(differentiation_hint(total))
            titles = cov.get("top_titles") or []
            if titles:
                st.caption("既存タイトル例（既に書かれた角度）:")
                for title in titles:
                    st.write(f"- {title}")
