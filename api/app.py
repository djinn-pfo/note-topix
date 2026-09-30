"""FastAPI backend for the note-topix dashboard.

Exposes the SQLite pipeline output as JSON. Meant to be served alongside the
Vite React app during development; in production, either serve the built
webapp as static files from FastAPI, or keep them as separate services
behind Tailscale.

Run locally:

    uv run uvicorn api.app:app --reload --port 8000

Bound to Tailscale:

    uv run uvicorn api.app:app --host $(tailscale ip -4 | head -1) --port 8000
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BRIEFINGS_DIR = REPO_ROOT / "documents" / "briefings"
MANUSCRIPTS_DIR = REPO_ROOT / "manuscripts"
ANNEX_DIR = REPO_ROOT / "documents" / "annex"
IMAGES_DIR = REPO_ROOT / "assets" / "images"

sys.path.insert(0, str(SCRIPTS_DIR))
from term_db import (  # noqa: E402
    PROPER_LABELS,
    article_stat_series,
    connect,
    list_articles,
    set_backlog_state,
    upsert_article,
    get_aliases,
    get_classifications,
    get_meta,
    list_community_names,
    members_hash,
    read_all_note_coverage,
    read_community_name,
    read_edges,
    read_signals,
    read_term_sets_in_range,
    read_term_stats_in_range,
    signal_ids_in_range,
    read_signals_for_term,
    read_term_stats,
    read_verbs_for_signals,
    signals_containing_terms,
)
from network_view import detect_communities  # noqa: E402
from articles import KINDS, challenge_progress, cmd_import as _articles_import, cmd_sync as _articles_sync
from backlog import STATES, list_backlog
from buffer_edges import BufferConfig, BufferNetwork, Corpus, build_network  # noqa: E402
from config import env  # noqa: E402


app = FastAPI(title="note-topix API", version="0.1.0")

# In dev, the Vite server is on a different port; permissive CORS is fine
# because the API is only exposed inside Tailscale.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


# ---------- Meta ----------


@app.get("/api/meta")
def api_meta() -> dict[str, Any]:
    conn = connect()
    try:
        signals = read_signals(conn)
        bucket_counts = Counter(s.get("bucket") or "" for s in signals)
        source_counts = Counter(s.get("source") or "" for s in signals)
        classifications = get_classifications(conn)
        by_label = Counter(v["label"] for v in classifications.values())
        dates = sorted(s["date"] for s in signals if s.get("date"))
        return {
            "last_run_at": get_meta(conn, "last_run_at"),
            "date_min": dates[0] if dates else None,
            "date_max": dates[-1] if dates else None,
            "signal_count": len(signals),
            "bucket_counts": dict(bucket_counts),
            "source_counts": dict(source_counts),
            "classifications": {
                "total": len(classifications),
                "by_label": dict(by_label),
            },
        }
    finally:
        conn.close()


# ---------- Terms ----------


@app.get("/api/terms")
def api_terms(
    mode: str = Query("all", pattern="^(all|propn)$"),
    min_occurrence: int = 1,
    limit: int = 500,
) -> list[dict[str, Any]]:
    conn = connect()
    try:
        stats = read_term_stats(conn, mode)
        classifications = get_classifications(conn)
        coverage = read_all_note_coverage(conn)
        out: list[dict[str, Any]] = []
        for term, meta in stats.items():
            if meta["occurrence"] < min_occurrence:
                continue
            cov = coverage.get(term)
            out.append(
                {
                    "term": term,
                    "occurrence": meta["occurrence"],
                    "engagement_sum": meta["engagement_sum"],
                    "buckets": sorted(meta["buckets"]),
                    "classification": classifications.get(term),
                    "note_coverage_total": cov.get("total") if cov else None,
                }
            )
        out.sort(key=lambda x: -x["occurrence"])
        return out[:limit]
    finally:
        conn.close()


# ---------- Graph ----------


@app.get("/api/graph")
def api_graph(
    mode: str = Query("all", pattern="^(all|propn)$"),
    method: str = Query("npmi", pattern="^(npmi|pmi|jaccard|count|buffer)$"),
    min_occurrence: int = 8,
    min_weight: float = 0.25,
    max_nodes: int = 150,
    community: bool = True,
    label_filter: list[str] | None = Query(None),
    window: int = Query(5, ge=1, le=20),
    include_verbs: bool = True,
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict[str, Any]:
    """Without a date range, co-occurrence edges come precomputed from the
    pipeline (whole corpus). With one, node stats and edges are recomputed over
    signals published in [date_from, date_to] (inclusive)."""
    dated = bool(date_from or date_to)
    conn = connect()
    try:
        if method == "buffer":
            return _buffer_graph(
                conn, min_occurrence, min_weight, max_nodes, community,
                label_filter, window, include_verbs, date_from, date_to,
            )
        stats = (
            read_term_stats_in_range(conn, mode, date_from, date_to) if dated
            else read_term_stats(conn, mode)
        )
        classifications = get_classifications(conn)

        # Optionally restrict to LLM-labeled terms.
        allowed_by_label: set[str] | None = None
        if label_filter:
            allowed_by_label = {
                t for t, meta in classifications.items()
                if meta.get("label") in label_filter
            }

        candidates = [
            (t, m) for t, m in stats.items()
            if m["occurrence"] >= min_occurrence
            and (allowed_by_label is None or t in allowed_by_label)
        ]
        # Term as tie-break so the node cutoff doesn't depend on dict order.
        candidates.sort(key=lambda p: (-p[1]["occurrence"], p[0]))
        node_terms = [t for t, _ in candidates[:max_nodes]]
        node_set = set(node_terms)

        # Filter edges.
        if dated:
            edges_raw = _edges_in_range(conn, mode, method, node_set, date_from, date_to)
        else:
            edges_raw, _ = read_edges(conn, mode, method)
        edges: list[dict[str, Any]] = []
        for pair, w in edges_raw.items():
            if w < min_weight:
                continue
            a, b = tuple(pair)
            if a in node_set and b in node_set:
                edges.append({"source": a, "target": b, "weight": float(w)})

        # Drop isolated nodes for a cleaner render.
        connected: set[str] = set()
        for e in edges:
            connected.add(e["source"])
            connected.add(e["target"])
        node_terms = [t for t in node_terms if t in connected]

        # Louvain
        node_to_com: dict[str, int] = {}
        if community and edges:
            g = nx.Graph()
            for e in edges:
                g.add_edge(e["source"], e["target"], weight=e["weight"])
            node_to_com = detect_communities(g)

        # Compose node payload.
        nodes = []
        for t in node_terms:
            meta = stats[t]
            nodes.append(
                {
                    "id": t,
                    "occurrence": meta["occurrence"],
                    "engagement_sum": meta["engagement_sum"],
                    "buckets": sorted(meta["buckets"]),
                    "dominant_bucket": (
                        max(meta["bucket_counts"], key=meta["bucket_counts"].get) if dated
                        else _dominant_bucket_for(conn, t, mode)
                    ),
                    "classification": classifications.get(t),
                    "community": node_to_com.get(t),
                }
            )

        # Community name lookup (if the current partition matches a cached hash)
        com_names = _community_names_for(conn, node_to_com) if node_to_com else {}

        return {
            "nodes": nodes,
            "edges": edges,
            "communities": com_names,
            "config": {
                "mode": mode, "method": method,
                "min_occurrence": min_occurrence, "min_weight": min_weight,
                "max_nodes": max_nodes,
                "community": community, "label_filter": label_filter,
                "date_from": date_from, "date_to": date_to,
            },
        }
    finally:
        conn.close()


def _edges_in_range(conn, mode: str, method: str, node_set: set[str], date_from, date_to):
    """Co-occurrence weights among ``node_set`` over signals in the period.
    Restricting to the displayed nodes keeps this fast; term and total counts
    are still over the whole period, so weights match a full recomputation."""
    from analyze_signals import edge_weights  # lazy: pulls in spaCy / Sudachi imports
    from run_pipeline import MIN_PAIR_COUNT

    sig_ids = signal_ids_in_range(conn, date_from, date_to)
    term_sets = read_term_sets_in_range(conn, mode, node_set, sig_ids)
    edges, _ = edge_weights(term_sets, method=method, min_pair_count=MIN_PAIR_COUNT)
    return edges


# ---------- Buffer-overlap graph (proper nouns linked via shared neighbors) ----------


_corpus_cache: dict[str, Corpus] = {}


def _buffer_network(
    conn, label_filter: list[str] | None, window: int, include_verbs: bool,
    date_from: str | None = None, date_to: str | None = None,
) -> BufferNetwork:
    # Slots only change when the pipeline reruns; labels / aliases / vocab are
    # small and re-read per request so LLM updates show up without a restart.
    run_key = get_meta(conn, "last_run_at") or ""
    corpus = _corpus_cache.get(run_key)
    if corpus is None:
        _corpus_cache.clear()
        corpus = _corpus_cache[run_key] = Corpus.load(conn)
    if date_from or date_to:
        keep = signal_ids_in_range(conn, date_from, date_to)
        corpus = Corpus({sid: seq for sid, seq in corpus.slots.items() if sid in keep}, corpus.bucket, corpus.engagement)
    core_labels = frozenset(label_filter) if label_filter else frozenset(PROPER_LABELS)
    config = BufferConfig(window=window, include_verbs=include_verbs, core_labels=core_labels)
    return build_network(conn, config, corpus)


def _community_names_for(conn, node_to_com: dict[str, int]) -> dict[int, dict]:
    by_com: dict[int, list[str]] = {}
    for t, cid in node_to_com.items():
        by_com.setdefault(cid, []).append(t)
    names: dict[int, dict] = {}
    for cid, members in by_com.items():
        cache = read_community_name(conn, members_hash(members))
        if cache:
            names[cid] = cache
    return names


def _buffer_graph(
    conn,
    min_occurrence: int,
    min_weight: float,
    max_nodes: int,
    community: bool,
    label_filter: list[str] | None,
    window: int,
    include_verbs: bool,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    net = _buffer_network(conn, label_filter, window, include_verbs, date_from, date_to)
    classifications = get_classifications(conn)
    node_terms, edges, node_to_com = net.partition(min_occurrence, min_weight, max_nodes, community)
    stats = {t: net.core_stats(t) for t in node_terms}

    nodes = []
    for t in node_terms:
        st = stats[t]
        nodes.append({
            "id": t,
            "occurrence": st["occurrence"],
            "engagement_sum": st["engagement_sum"],
            "buckets": st["buckets"],
            "dominant_bucket": st["dominant_bucket"],
            "classification": classifications.get(t) or {"label": st["label"], "confidence": None, "rationale": None},
            "community": node_to_com.get(t),
            "aliases": st["aliases"],
        })
    return {
        "nodes": nodes,
        "edges": edges,
        "communities": _community_names_for(conn, node_to_com) if node_to_com else {},
        "config": {
            "mode": "ai", "method": "buffer",
            "min_occurrence": min_occurrence, "min_weight": min_weight,
            "max_nodes": max_nodes, "community": community, "label_filter": label_filter,
            "window": window, "include_verbs": include_verbs,
            "date_from": date_from, "date_to": date_to,
        },
    }


@app.get("/api/buffer-profile")
def api_buffer_profile(
    term: str = Query(..., min_length=1),
    window: int = Query(5, ge=1, le=20),
    include_verbs: bool = True,
    label_filter: list[str] | None = Query(None),
    top_k: int = 20,
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict[str, Any]:
    """A core's top buffer features and its most similar cores (with the
    buffer features they share) — the "why" behind buffer edges."""
    conn = connect()
    try:
        net = _buffer_network(conn, label_filter, window, include_verbs, date_from, date_to)
        term = get_aliases(conn).get(term, term)
        if term not in net.cores:
            raise HTTPException(status_code=404, detail=f"{term!r} is not a core term")
        return {
            "term": term,
            "stats": net.core_stats(term),
            "profile": net.profile(term, top_k),
            "neighbors": net.neighbors(term, top_k=15),
        }
    finally:
        conn.close()


def _with_aliases(conn, term: str) -> list[str]:
    """A canonical core plus every alias that maps to it."""
    return [term, *(a for a, c in get_aliases(conn).items() if c == term)]


def _dominant_bucket_for(conn, term: str, mode: str) -> str:
    """Approximate the dominant bucket by counting signals containing the term."""
    sigs = read_signals_for_term(conn, term, mode)
    if not sigs:
        return ""
    counter = Counter(s.get("bucket") or "" for s in sigs)
    return counter.most_common(1)[0][0]


# ---------- Signals for term ----------


@app.get("/api/signals")
def api_signals_for_term(
    term: str = Query(..., min_length=1),
    mode: str = Query("all", pattern="^(all|propn)$"),
    limit: int = 30,
    sort: str = Query("engagement", pattern="^(engagement|date)$"),
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = {r["signal_id"]: r for t in _with_aliases(conn, term) for r in read_signals_for_term(conn, t, mode)}
        rows = [
            r for r in rows.values()
            if (not date_from or (r["date"] or "") >= date_from) and (not date_to or (r["date"] or "") <= date_to)
        ]
        if sort == "date":
            rows.sort(key=lambda r: r.get("date") or "", reverse=True)
        else:
            rows.sort(key=lambda r: r.get("engagement") or 0, reverse=True)
        return rows[:limit]
    finally:
        conn.close()


# ---------- Verbs for term (via signal_verbs join) ----------


@app.get("/api/verbs-for-term")
def api_verbs_for_term(
    term: str = Query(..., min_length=1),
    mode: str = Query("all", pattern="^(all|propn)$"),
    limit: int = 20,
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> list[dict[str, Any]]:
    conn = connect()
    try:
        sig_ids = signals_containing_terms(conn, _with_aliases(conn, term), mode)
        if date_from or date_to:
            in_range = signal_ids_in_range(conn, date_from, date_to)
            sig_ids = [s for s in sig_ids if s in in_range]
        counts = read_verbs_for_signals(conn, sig_ids)
        top = sorted(counts.items(), key=lambda x: -x[1])[:limit]
        return [{"verb": v, "count": c} for v, c in top]
    finally:
        conn.close()


# ---------- Communities ----------


@app.get("/api/communities")
def api_communities() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return list_community_names(conn)
    finally:
        conn.close()


# ---------- note.com coverage ----------


@app.get("/api/note-coverage")
def api_note_coverage() -> dict[str, dict]:
    conn = connect()
    try:
        return read_all_note_coverage(conn)
    finally:
        conn.close()


@app.get("/api/note-coverage/{term}")
def api_note_coverage_single(term: str) -> dict:
    conn = connect()
    try:
        cov = read_all_note_coverage(conn).get(term)
        if not cov:
            raise HTTPException(status_code=404, detail=f"no cached coverage for {term!r}")
        return cov
    finally:
        conn.close()


# ---------- Classifications ----------


@app.get("/api/classifications")
def api_classifications() -> dict[str, dict]:
    conn = connect()
    try:
        return get_classifications(conn)
    finally:
        conn.close()


# ---------- Briefings ----------


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


@app.get("/api/briefings")
def api_briefings() -> list[dict[str, Any]]:
    if not BRIEFINGS_DIR.exists():
        return []
    out = []
    for p in sorted(BRIEFINGS_DIR.glob("*.md"), reverse=True):
        stat = p.stat()
        out.append(
            {
                "name": p.name,
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            }
        )
    return out


@app.get("/api/briefings/{name}")
def api_briefing_content(name: str) -> dict[str, Any]:
    # Guard against path traversal.
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400, detail="invalid name")
    path = BRIEFINGS_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="briefing not found")
    text = path.read_text(encoding="utf-8")
    frontmatter: dict[str, Any] = {}
    body = text
    match = _FRONTMATTER_RE.match(text)
    if match:
        fm_raw, body = match.group(1), match.group(2).lstrip()
        for line in fm_raw.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            try:
                frontmatter[key] = json.loads(value)
            except json.JSONDecodeError:
                frontmatter[key] = value
    return {"name": name, "content": text, "body": body, "frontmatter": frontmatter}


# ---------- Manuscripts (writable) ----------


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._\-一-龯ぁ-んァ-ンー]+\.md$")


# The editor writes to two places: note drafts, and annex sources that
# publish_annex.py renders to the public site.
EDIT_DIRS = {"manuscripts": MANUSCRIPTS_DIR, "annex": ANNEX_DIR}


def _edit_dir(dir_key: str) -> Path:
    try:
        return EDIT_DIRS[dir_key]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown directory: {dir_key!r}") from None


def _validate_manuscript_name(name: str, dir_key: str = "manuscripts") -> Path:
    base = _edit_dir(dir_key)
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="invalid name (path segments not allowed)")
    if not _SAFE_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="invalid name (allowed: [A-Za-z0-9._-] plus JP characters, must end in .md)",
        )
    path = (base / name).resolve()
    base_resolved = base.resolve()
    if base_resolved not in path.parents and path != base_resolved:
        raise HTTPException(status_code=400, detail="path escapes the target directory")
    return path


@app.get("/api/manuscripts")
def api_manuscripts_list(
    dir: str = Query("manuscripts", pattern="^(manuscripts|annex)$"),
) -> list[dict[str, Any]]:
    base = _edit_dir(dir)
    if not base.exists():
        return []
    out = []
    for p in sorted(base.glob("*.md"), reverse=True):
        stat = p.stat()
        out.append(
            {
                "name": p.name,
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            }
        )
    return out


@app.get("/api/manuscripts/{name}")
def api_manuscript_get(
    name: str, dir: str = Query("manuscripts", pattern="^(manuscripts|annex)$"),
) -> dict[str, Any]:
    path = _validate_manuscript_name(name, dir)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="manuscript not found")
    text = path.read_text(encoding="utf-8")
    stat = path.stat()
    return {
        "name": name,
        "content": text,
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }


@app.put("/api/manuscripts/{name}")
def api_manuscript_save(
    name: str, payload: dict = Body(...),
    dir: str = Query("manuscripts", pattern="^(manuscripts|annex)$"),
) -> dict[str, Any]:
    path = _validate_manuscript_name(name, dir)
    content = payload.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")
    _edit_dir(dir).mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    stat = path.stat()
    return {
        "name": name,
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@app.delete("/api/manuscripts/{name}")
def api_manuscript_delete(
    name: str, dir: str = Query("manuscripts", pattern="^(manuscripts|annex)$"),
) -> dict[str, Any]:
    path = _validate_manuscript_name(name, dir)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="manuscript not found")
    path.unlink()
    return {"name": name, "deleted": True}


# ---------- Images (Gemini illustrations + hand-written SVG diagrams) ----------
#
# Each image lives in assets/images/ next to a <name>.json sidecar holding the
# prompt that produced it, so a picture can be regenerated or tweaked later.

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
IMAGE_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
}
_SAFE_IMAGE_NAME_RE = re.compile(r"^[\w.\- ()（）ぁ-んァ-ヶ一-龠々ー]+$", re.UNICODE)


def _image_path(name: str) -> Path:
    """Resolve a caller-supplied filename inside IMAGES_DIR, or 400/404."""
    if "/" in name or "\\" in name or name.startswith(".") or not _SAFE_IMAGE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"invalid image name: {name!r}")
    path = IMAGES_DIR / name
    if path.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(status_code=400, detail=f"unsupported extension: {path.suffix}")
    return path


def _meta_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".json")


def _read_meta(path: Path) -> dict:
    meta_file = _meta_path(path)
    if not meta_file.exists():
        return {}
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_meta(path: Path, meta: dict) -> None:
    _meta_path(path).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _image_entry(path: Path) -> dict[str, Any]:
    stat = path.stat()
    meta = _read_meta(path)
    return {
        "name": path.name,
        "url": f"/api/images/file/{path.name}",
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
        "kind": meta.get("kind") or ("diagram" if path.suffix.lower() == ".svg" else "illustration"),
        "prompt": meta.get("prompt", ""),
        "source": meta.get("source", ""),      # gemini / claude-svg / hand ...
        "article": meta.get("article", ""),    # which note article / annex it belongs to
        "created_at": meta.get("created_at", ""),
    }


def _unique_name(name: str) -> Path:
    path = _image_path(name)
    stem, suffix, i = path.stem, path.suffix, 2
    while path.exists():
        path = IMAGES_DIR / f"{stem}-{i}{suffix}"
        i += 1
    return path


@app.get("/api/images")
def api_images() -> list[dict[str, Any]]:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    entries = [
        _image_entry(p) for p in IMAGES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ]
    entries.sort(key=lambda e: -e["modified_at"])
    return entries


@app.get("/api/images/file/{name}")
def api_image_file(name: str) -> FileResponse:
    path = _image_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no such image: {name}")
    return FileResponse(path, media_type=IMAGE_MEDIA.get(path.suffix.lower(), "application/octet-stream"))


@app.post("/api/images")
async def api_image_upload(
    file: UploadFile = File(...),
    prompt: str = Form(""),
    source: str = Form("gemini"),
    article: str = Form(""),
    kind: str = Form(""),
) -> dict[str, Any]:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = _unique_name(Path(file.filename or "image.png").name)
    path.write_bytes(await file.read())
    _write_meta(path, {
        "prompt": prompt,
        "source": source,
        "article": article,
        "kind": kind or ("diagram" if path.suffix.lower() == ".svg" else "illustration"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return _image_entry(path)


@app.post("/api/images/svg")
def api_image_svg(payload: dict = Body(...)) -> dict[str, Any]:
    """Save a hand/Claude-written SVG diagram as a file plus its sidecar."""
    name = str(payload.get("name") or "diagram.svg")
    if not name.endswith(".svg"):
        name += ".svg"
    svg = str(payload.get("svg") or "")
    if "<svg" not in svg:
        raise HTTPException(status_code=400, detail="payload.svg is not an SVG document")
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = _image_path(name)
    if path.exists() and not payload.get("overwrite"):
        path = _unique_name(name)
    path.write_text(svg, encoding="utf-8")
    meta = _read_meta(path)
    _write_meta(path, {
        "prompt": payload.get("prompt", meta.get("prompt", "")),
        "source": payload.get("source", "claude-svg"),
        "article": payload.get("article", meta.get("article", "")),
        "kind": "diagram",
        "created_at": meta.get("created_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return _image_entry(path)


@app.get("/api/images/svg/{name}")
def api_image_svg_source(name: str) -> dict[str, Any]:
    path = _image_path(name)
    if not path.exists() or path.suffix.lower() != ".svg":
        raise HTTPException(status_code=404, detail=f"no such SVG: {name}")
    return {"name": path.name, "svg": path.read_text(encoding="utf-8"), **_image_entry(path)}


@app.put("/api/images/{name}")
def api_image_update(name: str, payload: dict = Body(...)) -> dict[str, Any]:
    path = _image_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no such image: {name}")
    meta = _read_meta(path)
    for field in ("prompt", "source", "article", "kind"):
        if field in payload:
            meta[field] = str(payload[field])
    meta.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    _write_meta(path, meta)
    return _image_entry(path)


@app.delete("/api/images/{name}")
def api_image_delete(name: str) -> dict[str, Any]:
    path = _image_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no such image: {name}")
    path.unlink()
    _meta_path(path).unlink(missing_ok=True)
    return {"name": name, "deleted": True}


# ---------- 100-day challenge: progress, articles, topic backlog ----------


@app.get("/api/challenge")
def api_challenge() -> dict[str, Any]:
    conn = connect()
    try:
        articles = list_articles(conn)
        return {
            "progress": challenge_progress(conn),
            "articles": articles,
            "kinds": list(KINDS),
            "states": list(STATES),
        }
    finally:
        conn.close()


@app.get("/api/articles/{article_id}/stats")
def api_article_stats(article_id: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return article_stat_series(conn, article_id)
    finally:
        conn.close()


@app.put("/api/articles/{article_id}")
def api_article_update(article_id: int, payload: dict = Body(...)) -> dict[str, Any]:
    """Attach the article format / topic after the fact (import only knows note's fields)."""
    conn = connect()
    try:
        rows = [a for a in list_articles(conn) if a["article_id"] == article_id]
        if not rows:
            raise HTTPException(status_code=404, detail=f"no such article: {article_id}")
        fields = {"note_key": rows[0]["note_key"]}
        for key in ("kind", "topic", "briefing", "annex_slug", "memo"):
            if key in payload:
                fields[key] = payload[key]
        if fields.get("kind") and fields["kind"] not in KINDS:
            raise HTTPException(status_code=400, detail=f"kind must be one of {KINDS}")
        upsert_article(conn, fields)
        if fields.get("briefing"):
            set_backlog_state(conn, fields["briefing"], "published", article_id=article_id)
        return [a for a in list_articles(conn) if a["article_id"] == article_id][0]
    finally:
        conn.close()


@app.post("/api/articles/refresh")
def api_articles_refresh(payload: dict = Body(default={})) -> dict[str, Any]:
    """Pull newly published notes and refresh スキ counts (note's public API)."""
    import argparse as _argparse

    args = _argparse.Namespace(
        user=payload.get("user") or env("NOTE_USER"),
        max_pages=int(payload.get("max_pages") or 3), delay=0.4,
    )
    if not args.user:
        raise HTTPException(status_code=400, detail="note のユーザー名が未設定です (.env の NOTE_USER)")
    _articles_import(args)
    conn = connect()
    try:
        return {"progress": challenge_progress(conn), "articles": list_articles(conn)}
    finally:
        conn.close()


@app.get("/api/backlog")
def api_backlog() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return list_backlog(conn)
    finally:
        conn.close()


@app.put("/api/backlog")
def api_backlog_update(payload: dict = Body(...)) -> dict[str, Any]:
    briefing = str(payload.get("briefing") or "")
    state = str(payload.get("state") or "")
    if not briefing or state not in STATES:
        raise HTTPException(status_code=400, detail=f"briefing required, state must be one of {STATES}")
    conn = connect()
    try:
        set_backlog_state(conn, briefing, state, payload.get("note_kind"), payload.get("memo"))
        return next((r for r in list_backlog(conn) if r["briefing"] == briefing), {"briefing": briefing, "state": state})
    finally:
        conn.close()


# ---------- Publish kit (X post, tags, diagram ideas) ----------

PUBLISH_KIT_PROMPT = """あなたは note で技術記事を書く研究者・エンジニアの編集担当です。
以下の記事の下書きから、公開作業に使う3点を作ります。

# 下書き

{content}

---

# 出力

JSON のみを出力してください（前置き・コードフェンスなし）。

{{
  "x_post": "X(Twitter)用の投稿文。記事の要点を3行以内。煽らず、何が分かる記事かを具体的に。末尾に改行してリンクを置く場所として {{URL}} と書く。140字程度",
  "tags": ["note のハッシュタグを5個。中身に即したものだけ。#は付けない"],
  "title_candidates": ["タイトル案を3つ。40字前後で、固有名詞と具体を含める"],
  "diagrams": [
    {{
      "title": "この記事に入れると効く図の名前",
      "why": "その図で読者の何が分かるようになるか（1文）",
      "svg": "<svg xmlns=\\"http://www.w3.org/2000/svg\\" viewBox=\\"0 0 640 360\\" width=\\"640\\" height=\\"360\\">…</svg>"
    }}
  ]
}}

# 制約

- 記事に書かれていないことを足さない
- diagrams は1〜2個。**構造や流れを示す図**にする（棒グラフのような装飾は不要）
- SVG は白背景、日本語ラベル、font-size 14 以上、viewBox 0 0 640 360 で、そのまま
  表示できる完成物にする。図形は矩形・矢印・テキスト中心の素朴なもので良い
- 一人称（私は/筆者は）は使わない
"""


@app.post("/api/publish-kit")
def api_publish_kit(payload: dict = Body(...)) -> dict[str, Any]:
    """Draft the X post, tags, title options and diagram skeletons for a draft."""
    content = str(payload.get("content") or "").strip()
    if len(content) < 200:
        raise HTTPException(status_code=400, detail="下書きが短すぎます (200文字以上)")
    from llm import call_claude_json

    try:
        kit = call_claude_json(PUBLISH_KIT_PROMPT.format(content=content[:12000]), timeout=600)
    except Exception as exc:  # noqa: BLE001 — surface the CLI error to the UI
        raise HTTPException(status_code=502, detail=f"claude -p 失敗: {exc}") from None
    if not isinstance(kit, dict):
        raise HTTPException(status_code=502, detail="想定外の出力形式")
    return kit
