"""SQLite-backed term registry with LLM classifications.

Two tables:
  - ``terms``           : distinct terms observed across all signals + occurrence
  - ``classifications`` : Claude-provided label for each term (cached, so re-runs
                          only pay for newly-seen terms)

Rationale: dictionary-based POS taggers miss emerging entities (MCP, Anthropic,
DeepSeek, ...). Delegating classification to Claude uses up-to-date world
knowledge without maintaining a domain dictionary.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "knowledges" / "terms.db"

# Label vocabulary. Kept small so subagents produce it reliably.
LABELS = {
    "PROPER_PRODUCT",  # specific software / model / product ("Claude Code", "GPT-5", "Ollama")
    "PROPER_ORG",      # company / lab / team ("Anthropic", "松尾研")
    "PROPER_PERSON",   # person name
    "PROPER_PLACE",    # geographic entity
    "PROPER_WORK",     # paper / book / event / standard ("MCP", "ICML 2026")
    "CONCEPT",         # technical concept ("prompt injection", "強化学習")
    "GENERIC",         # common noun with no specific entity referent
    "JUNK",            # tokenization artifact, non-word
}
PROPER_LABELS = {l for l in LABELS if l.startswith("PROPER_")}


SCHEMA = """
CREATE TABLE IF NOT EXISTS terms (
    term            TEXT PRIMARY KEY,
    lang            TEXT,
    occurrence      INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_terms_occurrence ON terms(occurrence DESC);

CREATE TABLE IF NOT EXISTS classifications (
    term          TEXT PRIMARY KEY REFERENCES terms(term),
    label         TEXT NOT NULL,
    confidence    REAL,
    rationale     TEXT,
    classified_at TEXT NOT NULL,
    model         TEXT
);
CREATE INDEX IF NOT EXISTS idx_class_label ON classifications(label);

-- Frozen snapshot of the signal corpus at pipeline-run time.
CREATE TABLE IF NOT EXISTS signals (
    signal_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT UNIQUE NOT NULL,
    source      TEXT,
    bucket      TEXT,
    title       TEXT,
    url         TEXT,
    body        TEXT,
    date        TEXT,
    engagement  REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_source ON signals(source);

-- Per-term stats broken down by noun_mode (which POS filter produced it).
CREATE TABLE IF NOT EXISTS term_stats (
    term            TEXT NOT NULL,
    noun_mode       TEXT NOT NULL,
    occurrence      INTEGER NOT NULL,
    engagement_sum  REAL NOT NULL DEFAULT 0,
    buckets         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (term, noun_mode)
);
CREATE INDEX IF NOT EXISTS idx_term_stats_mode_occ ON term_stats(noun_mode, occurrence DESC);

-- Which signals contain a given term (per noun_mode).
CREATE TABLE IF NOT EXISTS signal_terms (
    signal_id   INTEGER NOT NULL REFERENCES signals(signal_id) ON DELETE CASCADE,
    term        TEXT NOT NULL,
    noun_mode   TEXT NOT NULL,
    PRIMARY KEY (signal_id, term, noun_mode)
);
CREATE INDEX IF NOT EXISTS idx_signal_terms_lookup ON signal_terms(term, noun_mode);

-- Pre-computed pairwise edges for every (noun_mode, method) combination.
CREATE TABLE IF NOT EXISTS edges (
    term_a      TEXT NOT NULL,
    term_b      TEXT NOT NULL,
    noun_mode   TEXT NOT NULL,
    method      TEXT NOT NULL,
    weight      REAL NOT NULL,
    pair_count  INTEGER NOT NULL,
    PRIMARY KEY (term_a, term_b, noun_mode, method),
    CHECK (term_a < term_b)
);
CREATE INDEX IF NOT EXISTS idx_edges_mode_method ON edges(noun_mode, method, weight DESC);

-- Cached note.com paid-note coverage per term.
CREATE TABLE IF NOT EXISTS note_coverage (
    term         TEXT PRIMARY KEY,
    total        INTEGER,
    rounded      INTEGER,
    top_titles   TEXT,
    fetched_at   TEXT NOT NULL
);

-- Lemma-form verbs that appeared in each signal (for community naming).
CREATE TABLE IF NOT EXISTS signal_verbs (
    signal_id   INTEGER NOT NULL REFERENCES signals(signal_id) ON DELETE CASCADE,
    verb        TEXT NOT NULL,
    PRIMARY KEY (signal_id, verb)
);
CREATE INDEX IF NOT EXISTS idx_signal_verbs_lookup ON signal_verbs(verb);

-- Cached community names produced by subagents (per Louvain config).
CREATE TABLE IF NOT EXISTS community_names (
    members_hash    TEXT PRIMARY KEY,   -- sha1 of sorted member terms
    config_json     TEXT NOT NULL,      -- {"mode":..., "method":..., ...}
    members_json    TEXT NOT NULL,      -- JSON list of member terms
    top_verbs_json  TEXT,               -- JSON list of frequent verbs
    name            TEXT NOT NULL,
    description     TEXT,
    created_at      TEXT NOT NULL,
    model           TEXT
);

-- Positioned noun runs / verbs per signal (noun_mode 'all'). Feeds the
-- buffer-overlap network, which needs each proper-noun mention's neighbors.
CREATE TABLE IF NOT EXISTS signal_slots (
    signal_id   INTEGER NOT NULL REFERENCES signals(signal_id) ON DELETE CASCADE,
    off         INTEGER NOT NULL,   -- char offset
    sent        INTEGER NOT NULL,   -- sentence index
    kind        TEXT NOT NULL,      -- 'n' noun run | 'v' verb
    units       TEXT NOT NULL       -- tab-joined units (noun: tokens + compound)
);
CREATE INDEX IF NOT EXISTS idx_signal_slots_signal ON signal_slots(signal_id);

-- LLM-provided canonical form for proper-noun variants ("hokusai",
-- "katsushika" -> "katsushika hokusai"). Preserved across pipeline runs.
CREATE TABLE IF NOT EXISTS term_aliases (
    alias       TEXT PRIMARY KEY,
    canonical   TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    model       TEXT
);

-- LLM judgment of whether a common noun is topic-bearing enough to serve as
-- a buffer feature (ok=1) or is filler / template noise (ok=0).
CREATE TABLE IF NOT EXISTS buffer_vocab (
    term        TEXT PRIMARY KEY,
    ok          INTEGER NOT NULL,
    updated_at  TEXT NOT NULL,
    model       TEXT
);

-- Auto-generated community briefings (generate_briefing.py auto). Logged in
-- the DB rather than inferred from files so that deleting an unwanted
-- briefing doesn't make the next run regenerate it.
CREATE TABLE IF NOT EXISTS briefing_log (
    members_hash    TEXT PRIMARY KEY,
    community_name  TEXT NOT NULL,
    members_json    TEXT NOT NULL,
    path            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    model           TEXT
);

-- ---------- Time-series history ----------
-- Unlike the derived tables above, these are never wiped: each pipeline run
-- appends a snapshot, so topics that keep coming back (deep-dive candidates)
-- can be told apart from one-off spikes.

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT NOT NULL UNIQUE,  -- = meta.last_run_at of that run
    signal_count  INTEGER NOT NULL,
    view_json     TEXT                   -- buffer view used for the snapshots
);

-- A community's identity across runs. Members drift as signals accumulate,
-- which changes members_hash; a lineage chains snapshots whose members
-- overlap (Jaccard) with a recent snapshot of the same lineage.
CREATE TABLE IF NOT EXISTS community_lineages (
    lineage_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    first_run_id   INTEGER NOT NULL REFERENCES pipeline_runs(run_id),
    last_run_id    INTEGER NOT NULL REFERENCES pipeline_runs(run_id),
    name           TEXT                  -- latest known community name
);

CREATE TABLE IF NOT EXISTS community_snapshots (
    run_id        INTEGER NOT NULL REFERENCES pipeline_runs(run_id),
    lineage_id    INTEGER NOT NULL REFERENCES community_lineages(lineage_id),
    members_hash  TEXT NOT NULL,
    name          TEXT,
    size          INTEGER NOT NULL,
    signal_count  INTEGER NOT NULL,      -- signals mentioning any member (cumulative)
    bucket_count  INTEGER NOT NULL,      -- independent source buckets among them
    members_json  TEXT NOT NULL,
    buffers_json  TEXT,                  -- shared buffer features at that time
    PRIMARY KEY (run_id, members_hash)
);
CREATE INDEX IF NOT EXISTS idx_community_snapshots_lineage ON community_snapshots(lineage_id, run_id);

-- Per-run stats for every proper-noun core in the buffer network.
CREATE TABLE IF NOT EXISTS core_snapshots (
    run_id        INTEGER NOT NULL REFERENCES pipeline_runs(run_id),
    term          TEXT NOT NULL,         -- canonical form
    occurrence    INTEGER NOT NULL,      -- signals mentioning it
    mentions      INTEGER NOT NULL,
    buckets       TEXT NOT NULL,
    lineage_id    INTEGER,               -- community it sat in (NULL = not in the graph)
    PRIMARY KEY (run_id, term)
);
CREATE INDEX IF NOT EXISTS idx_core_snapshots_term ON core_snapshots(term, run_id);

-- note_coverage keeps only the latest value; this keeps every fetch so
-- "rising but still under-covered" can be judged over time.
CREATE TABLE IF NOT EXISTS note_coverage_history (
    term        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    total       INTEGER,
    PRIMARY KEY (term, fetched_at)
);

-- Daily mentions by publication date. Signal files are never deleted, so this
-- is always recomputable from the current corpus and needs no snapshot.
CREATE VIEW IF NOT EXISTS term_daily AS
SELECT st.term, st.noun_mode, s.date AS day,
       COUNT(*) AS signals, COUNT(DISTINCT s.bucket) AS buckets
  FROM signal_terms st JOIN signals s USING (signal_id)
 GROUP BY st.term, st.noun_mode, s.date;

-- ---------- 100-day posting challenge ----------

-- Published note articles. Registered by URL; title/date/price come from the
-- note API. `kind` is the article format (why / news / retro / ...), used to
-- check later which formats actually landed.
CREATE TABLE IF NOT EXISTS articles (
    article_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    note_key      TEXT UNIQUE,
    url           TEXT,
    title         TEXT NOT NULL,
    published_on  TEXT,            -- YYYY-MM-DD, local date of publication
    kind          TEXT,
    topic         TEXT,            -- community name / topic label
    briefing      TEXT,            -- source briefing path
    annex_slug    TEXT,
    price         INTEGER NOT NULL DEFAULT 0,
    memo          TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_on);

-- One row per stats fetch, so the growth curve of スキ is kept, not just the
-- latest number.
CREATE TABLE IF NOT EXISTS article_stats (
    article_id  INTEGER NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
    fetched_at  TEXT NOT NULL,
    likes       INTEGER,
    comments    INTEGER,
    PRIMARY KEY (article_id, fetched_at)
);

-- Which briefings are still candidates, being written, or done. Keyed by the
-- briefing file so the daily pipeline output stays the source of truth.
CREATE TABLE IF NOT EXISTS topic_backlog (
    briefing    TEXT PRIMARY KEY,
    state       TEXT NOT NULL,     -- candidate | researched | writing | published | dropped
    note_kind   TEXT,              -- planned article format
    memo        TEXT,
    article_id  INTEGER REFERENCES articles(article_id),
    updated_at  TEXT NOT NULL
);

-- Free-form pipeline metadata (last_run_at, signal_count, timings...).
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def detect_lang(term: str) -> str:
    has_ascii_letter = bool(re.search(r"[A-Za-z]", term))
    has_cjk = bool(re.search(r"[぀-ヿ㐀-鿿]", term))
    if has_ascii_letter and has_cjk:
        return "mixed"
    if has_cjk:
        return "ja"
    if has_ascii_letter:
        return "en"
    return "other"


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(community_names)")}
    if "buffers_json" not in cols:
        # Shared buffer features that tie a buffer-method community together.
        conn.execute("ALTER TABLE community_names ADD COLUMN buffers_json TEXT")
        conn.commit()


def upsert_terms(conn: sqlite3.Connection, term_to_occurrence: dict[str, int]) -> tuple[int, int]:
    """Insert new terms, refresh occurrence for existing. Returns (new, updated)."""
    now = _now()
    new_count = 0
    updated_count = 0
    for term, occurrence in term_to_occurrence.items():
        cur = conn.execute("SELECT 1 FROM terms WHERE term = ?", (term,))
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO terms(term, lang, occurrence, first_seen_at, updated_at) VALUES (?,?,?,?,?)",
                (term, detect_lang(term), occurrence, now, now),
            )
            new_count += 1
        else:
            conn.execute(
                "UPDATE terms SET occurrence = ?, updated_at = ? WHERE term = ?",
                (occurrence, now, term),
            )
            updated_count += 1
    conn.commit()
    return new_count, updated_count


def list_unclassified(
    conn: sqlite3.Connection,
    min_occurrence: int = 1,
    limit: int | None = None,
) -> list[tuple[str, int, str]]:
    """Return [(term, occurrence, lang)] with no classification, filtered by min occurrence."""
    sql = """
        SELECT t.term, t.occurrence, t.lang
          FROM terms t
     LEFT JOIN classifications c ON c.term = t.term
         WHERE c.term IS NULL AND t.occurrence >= ?
      ORDER BY t.occurrence DESC
    """
    params: list = [min_occurrence]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def save_classification(
    conn: sqlite3.Connection,
    term: str,
    label: str,
    confidence: float | None = None,
    rationale: str | None = None,
    model: str | None = None,
) -> None:
    if label not in LABELS:
        raise ValueError(f"unknown label: {label}")
    conn.execute(
        """
        INSERT INTO classifications(term, label, confidence, rationale, classified_at, model)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(term) DO UPDATE SET
            label = excluded.label,
            confidence = excluded.confidence,
            rationale = excluded.rationale,
            classified_at = excluded.classified_at,
            model = excluded.model
        """,
        (term, label, confidence, rationale, _now(), model),
    )


def save_classifications_bulk(
    conn: sqlite3.Connection, rows: list[dict], model: str | None = None
) -> int:
    """Bulk save. Each row must have term + label; confidence/rationale optional."""
    n = 0
    for row in rows:
        try:
            save_classification(
                conn,
                term=row["term"],
                label=row["label"],
                confidence=row.get("confidence"),
                rationale=row.get("rationale"),
                model=model,
            )
            n += 1
        except (KeyError, ValueError):
            continue
    conn.commit()
    return n


def get_classifications(
    conn: sqlite3.Connection, terms: list[str] | None = None
) -> dict[str, dict]:
    """Return {term: {label, confidence, rationale}} for classified terms."""
    if terms is None:
        cur = conn.execute("SELECT term, label, confidence, rationale FROM classifications")
    else:
        placeholders = ",".join("?" * len(terms))
        cur = conn.execute(
            f"SELECT term, label, confidence, rationale FROM classifications WHERE term IN ({placeholders})",
            terms,
        )
    return {
        row[0]: {"label": row[1], "confidence": row[2], "rationale": row[3]}
        for row in cur
    }


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
    classified = conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
    by_label = dict(conn.execute(
        "SELECT label, COUNT(*) FROM classifications GROUP BY label ORDER BY COUNT(*) DESC"
    ))
    return {"total_terms": total, "classified": classified, "by_label": by_label}


# ---------- Pipeline persistence ----------


def clear_pipeline_data(conn: sqlite3.Connection) -> None:
    """Wipe pipeline-derived tables before a fresh run. `terms`,
    `classifications`, and `community_names` are preserved (all LLM investments).
    """
    for table in ("signal_terms", "signal_verbs", "signal_slots", "edges", "term_stats", "signals"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def persist_signal_verbs(
    conn: sqlite3.Connection,
    signals: list,
    verb_sets: list[set[str]],
    path_to_id: dict[str, int],
) -> None:
    rows: list[tuple[int, str]] = []
    for signal, verbs in zip(signals, verb_sets):
        sid = path_to_id.get(str(signal.path))
        if sid is None:
            continue
        for verb in verbs:
            rows.append((sid, verb))
    conn.executemany(
        "INSERT OR IGNORE INTO signal_verbs(signal_id, verb) VALUES (?,?)",
        rows,
    )
    conn.commit()


def read_verbs_for_signals(
    conn: sqlite3.Connection, signal_ids: list[int]
) -> dict[str, int]:
    """Return {verb: total_occurrence} across the given signals."""
    if not signal_ids:
        return {}
    placeholders = ",".join("?" * len(signal_ids))
    counter: dict[str, int] = {}
    for (verb,) in conn.execute(
        f"SELECT verb FROM signal_verbs WHERE signal_id IN ({placeholders})",
        signal_ids,
    ):
        counter[verb] = counter.get(verb, 0) + 1
    return counter


def signals_containing_terms(
    conn: sqlite3.Connection, terms: list[str], noun_mode: str
) -> list[int]:
    if not terms:
        return []
    placeholders = ",".join("?" * len(terms))
    rows = conn.execute(
        f"""
        SELECT DISTINCT signal_id
          FROM signal_terms
         WHERE term IN ({placeholders}) AND noun_mode = ?
        """,
        [*terms, noun_mode],
    ).fetchall()
    return [r[0] for r in rows]


def save_community_name(
    conn: sqlite3.Connection,
    members_hash: str,
    config: dict,
    members: list[str],
    top_verbs: list[tuple[str, int]],
    name: str,
    description: str | None = None,
    model: str | None = None,
    buffers: list[str] | None = None,
) -> None:
    import json as _json
    conn.execute(
        """
        INSERT INTO community_names(members_hash, config_json, members_json, top_verbs_json, name, description, created_at, model, buffers_json)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(members_hash) DO UPDATE SET
            name = excluded.name,
            description = excluded.description,
            top_verbs_json = excluded.top_verbs_json,
            created_at = excluded.created_at,
            model = excluded.model,
            buffers_json = excluded.buffers_json
        """,
        (
            members_hash,
            _json.dumps(config, ensure_ascii=False),
            _json.dumps(members, ensure_ascii=False),
            _json.dumps(top_verbs, ensure_ascii=False),
            name,
            description,
            _now(),
            model,
            _json.dumps(buffers, ensure_ascii=False) if buffers is not None else None,
        ),
    )
    conn.commit()


def read_community_name(
    conn: sqlite3.Connection, members_hash: str
) -> dict | None:
    import json as _json
    row = conn.execute(
        "SELECT name, description, top_verbs_json, buffers_json FROM community_names WHERE members_hash = ?",
        (members_hash,),
    ).fetchone()
    if not row:
        return None
    return {
        "name": row[0],
        "description": row[1],
        "top_verbs": _json.loads(row[2]) if row[2] else [],
        "buffers": _json.loads(row[3]) if row[3] else [],
    }


def list_community_names(conn: sqlite3.Connection) -> list[dict]:
    import json as _json
    out: list[dict] = []
    for mhash, name, desc, members_json, verbs_json, config_json, created_at, buffers_json in conn.execute(
        """
        SELECT members_hash, name, description, members_json, top_verbs_json, config_json, created_at, buffers_json
          FROM community_names ORDER BY name
        """
    ):
        out.append(
            {
                "members_hash": mhash,
                "buffers": _json.loads(buffers_json) if buffers_json else [],
                "name": name,
                "description": desc,
                "members": _json.loads(members_json) if members_json else [],
                "top_verbs": _json.loads(verbs_json) if verbs_json else [],
                "config": _json.loads(config_json) if config_json else {},
                "created_at": created_at,
            }
        )
    return out


def save_briefing_log(
    conn: sqlite3.Connection,
    members_hash: str,
    community_name: str,
    members: list[str],
    path: str,
    model: str | None = None,
) -> None:
    import json as _json
    conn.execute(
        """
        INSERT OR REPLACE INTO briefing_log(members_hash, community_name, members_json, path, created_at, model)
        VALUES (?,?,?,?,?,?)
        """,
        (members_hash, community_name, _json.dumps(members, ensure_ascii=False), path, _now(), model),
    )
    conn.commit()


def list_briefing_log(conn: sqlite3.Connection) -> list[dict]:
    import json as _json
    return [
        {
            "members_hash": h,
            "community_name": n,
            "members": _json.loads(m),
            "path": p,
            "created_at": c,
        }
        for h, n, m, p, c in conn.execute(
            "SELECT members_hash, community_name, members_json, path, created_at FROM briefing_log"
        )
    ]


# ---------- Articles / backlog ----------


def upsert_article(conn: sqlite3.Connection, fields: dict) -> int:
    """Insert or update by note_key. Returns article_id."""
    row = conn.execute(
        "SELECT article_id FROM articles WHERE note_key = ?", (fields.get("note_key"),)
    ).fetchone()
    cols = ("note_key", "url", "title", "published_on", "kind", "topic", "briefing",
            "annex_slug", "price", "memo")
    if row:
        sets = ", ".join(f"{c} = ?" for c in cols if c in fields)
        params = [fields[c] for c in cols if c in fields]
        conn.execute(f"UPDATE articles SET {sets} WHERE article_id = ?", [*params, row[0]])
        conn.commit()
        return row[0]
    values = {c: fields.get(c) for c in cols}
    values["price"] = int(values.get("price") or 0)
    cur = conn.execute(
        f"INSERT INTO articles({', '.join(cols)}, created_at) VALUES ({', '.join('?' * len(cols))}, ?)",
        [*[values[c] for c in cols], _now()],
    )
    conn.commit()
    return cur.lastrowid


def record_article_stats(conn: sqlite3.Connection, article_id: int, likes: int, comments: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO article_stats(article_id, fetched_at, likes, comments) VALUES (?,?,?,?)",
        (article_id, _now(), likes, comments),
    )
    conn.commit()


def list_articles(conn: sqlite3.Connection) -> list[dict]:
    """Articles newest-first, each with its latest stats and the 24h delta."""
    rows = conn.execute(
        """
        SELECT a.article_id, a.note_key, a.url, a.title, a.published_on, a.kind, a.topic,
               a.briefing, a.annex_slug, a.price, a.memo,
               (SELECT likes FROM article_stats s WHERE s.article_id = a.article_id
                 ORDER BY fetched_at DESC LIMIT 1),
               (SELECT comments FROM article_stats s WHERE s.article_id = a.article_id
                 ORDER BY fetched_at DESC LIMIT 1),
               (SELECT fetched_at FROM article_stats s WHERE s.article_id = a.article_id
                 ORDER BY fetched_at DESC LIMIT 1),
               (SELECT likes FROM article_stats s WHERE s.article_id = a.article_id
                 AND s.fetched_at <= datetime('now', '-1 day') ORDER BY fetched_at DESC LIMIT 1)
          FROM articles a
         ORDER BY a.published_on DESC, a.article_id DESC
        """
    ).fetchall()
    keys = ("article_id", "note_key", "url", "title", "published_on", "kind", "topic",
            "briefing", "annex_slug", "price", "memo", "likes", "comments",
            "stats_fetched_at", "likes_24h_ago")
    return [dict(zip(keys, r)) for r in rows]


def article_stat_series(conn: sqlite3.Connection, article_id: int) -> list[dict]:
    return [
        {"fetched_at": f, "likes": l, "comments": c}
        for f, l, c in conn.execute(
            "SELECT fetched_at, likes, comments FROM article_stats WHERE article_id = ? ORDER BY fetched_at",
            (article_id,),
        )
    ]


def set_backlog_state(
    conn: sqlite3.Connection, briefing: str, state: str,
    note_kind: str | None = None, memo: str | None = None, article_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO topic_backlog(briefing, state, note_kind, memo, article_id, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(briefing) DO UPDATE SET
            state = excluded.state,
            note_kind = COALESCE(excluded.note_kind, topic_backlog.note_kind),
            memo = COALESCE(excluded.memo, topic_backlog.memo),
            article_id = COALESCE(excluded.article_id, topic_backlog.article_id),
            updated_at = excluded.updated_at
        """,
        (briefing, state, note_kind, memo, article_id, _now()),
    )
    conn.commit()


def get_backlog(conn: sqlite3.Connection) -> dict[str, dict]:
    return {
        r[0]: {"state": r[1], "note_kind": r[2], "memo": r[3], "article_id": r[4], "updated_at": r[5]}
        for r in conn.execute(
            "SELECT briefing, state, note_kind, memo, article_id, updated_at FROM topic_backlog"
        )
    }


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def members_hash(members: list[str]) -> str:
    import hashlib
    return hashlib.sha1(",".join(sorted(members)).encode("utf-8")).hexdigest()[:16]


def persist_signals(conn: sqlite3.Connection, signals: list) -> dict[str, int]:
    """Insert signals, return {path: signal_id} map for signal_terms writes."""
    path_to_id: dict[str, int] = {}
    for s in signals:
        cur = conn.execute(
            """
            INSERT INTO signals(path, source, bucket, title, url, body, date, engagement)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                str(s.path),
                s.source,
                s.bucket,
                s.title,
                getattr(s, "url", "") or "",
                s.body,
                s.date,
                float(s.engagement),
            ),
        )
        path_to_id[str(s.path)] = cur.lastrowid
    conn.commit()
    return path_to_id


def persist_term_stats(
    conn: sqlite3.Connection, noun_mode: str, index: dict
) -> None:
    rows = []
    for term, stats in index.items():
        engagement = sum(s.engagement for s in stats.signals)
        buckets = ",".join(sorted(stats.buckets))
        rows.append((term, noun_mode, stats.count, engagement, buckets))
    conn.executemany(
        "INSERT INTO term_stats(term, noun_mode, occurrence, engagement_sum, buckets) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()


def persist_signal_terms(
    conn: sqlite3.Connection,
    noun_mode: str,
    signals: list,
    term_sets: list[set[str]],
    path_to_id: dict[str, int],
) -> None:
    rows: list[tuple[int, str, str]] = []
    for signal, terms in zip(signals, term_sets):
        sid = path_to_id.get(str(signal.path))
        if sid is None:
            continue
        for term in terms:
            rows.append((sid, term, noun_mode))
    conn.executemany(
        "INSERT INTO signal_terms(signal_id, term, noun_mode) VALUES (?,?,?)",
        rows,
    )
    conn.commit()


def persist_edges(
    conn: sqlite3.Connection,
    noun_mode: str,
    method: str,
    edges: dict[frozenset[str], float],
    pair_counts: dict[frozenset[str], int],
) -> None:
    rows: list[tuple[str, str, str, str, float, int]] = []
    for pair, weight in edges.items():
        a, b = sorted(pair)
        rows.append((a, b, noun_mode, method, float(weight), int(pair_counts.get(pair, 0))))
    conn.executemany(
        """
        INSERT INTO edges(term_a, term_b, noun_mode, method, weight, pair_count)
        VALUES (?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()


def persist_signal_slots(
    conn: sqlite3.Connection,
    signals: list,
    slot_lists: list[list],
    path_to_id: dict[str, int],
) -> None:
    rows: list[tuple[int, int, int, str, str]] = []
    for signal, slots in zip(signals, slot_lists):
        sid = path_to_id.get(str(signal.path))
        if sid is None:
            continue
        for sl in slots:
            rows.append((sid, sl.off, sl.sent, sl.kind, "\t".join(sl.units)))
    conn.executemany(
        "INSERT INTO signal_slots(signal_id, off, sent, kind, units) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()


def read_signal_slots(
    conn: sqlite3.Connection,
) -> dict[int, list[tuple[int, int, str, tuple[str, ...]]]]:
    """{signal_id: [(off, sent, kind, units), ...]} in text order."""
    out: dict[int, list[tuple[int, int, str, tuple[str, ...]]]] = {}
    for sid, off, sent, kind, units in conn.execute(
        "SELECT signal_id, off, sent, kind, units FROM signal_slots ORDER BY signal_id, off, kind"
    ):
        out.setdefault(sid, []).append((off, sent, kind, tuple(units.split("\t"))))
    return out


def get_aliases(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT alias, canonical FROM term_aliases"))


def replace_aliases(
    conn: sqlite3.Connection, groups: list[dict], model: str | None = None
) -> int:
    """Replace the whole alias table with ``[{canonical, aliases: [...]}, ...]``.

    Grouping is a global judgment (one LLM pass over all proper nouns), so a
    full replace is simpler than merging partial group updates.
    """
    now = _now()
    rows = []
    for g in groups:
        canonical = g["canonical"]
        for alias in g.get("aliases", []):
            if alias != canonical:
                rows.append((alias, canonical, now, model))
    conn.execute("DELETE FROM term_aliases")
    conn.executemany(
        "INSERT OR REPLACE INTO term_aliases(alias, canonical, updated_at, model) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def get_buffer_vocab(conn: sqlite3.Connection) -> dict[str, bool]:
    return {t: bool(ok) for t, ok in conn.execute("SELECT term, ok FROM buffer_vocab")}


def save_buffer_vocab_bulk(
    conn: sqlite3.Connection, judgments: dict[str, bool], model: str | None = None
) -> int:
    now = _now()
    conn.executemany(
        """
        INSERT INTO buffer_vocab(term, ok, updated_at, model) VALUES (?,?,?,?)
        ON CONFLICT(term) DO UPDATE SET
            ok = excluded.ok, updated_at = excluded.updated_at, model = excluded.model
        """,
        [(t, int(ok), now, model) for t, ok in judgments.items()],
    )
    conn.commit()
    return len(judgments)


def persist_note_coverage(conn: sqlite3.Connection, term: str, coverage: dict | None) -> None:
    if coverage is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO note_coverage_history(term, fetched_at, total) VALUES (?,?,?)",
        (term, _now(), coverage.get("total")),
    )
    conn.execute(
        """
        INSERT INTO note_coverage(term, total, rounded, top_titles, fetched_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(term) DO UPDATE SET
            total = excluded.total,
            rounded = excluded.rounded,
            top_titles = excluded.top_titles,
            fetched_at = excluded.fetched_at
        """,
        (
            term,
            coverage.get("total"),
            coverage.get("rounded"),
            __import__("json").dumps(coverage.get("top_titles") or [], ensure_ascii=False),
            _now(),
        ),
    )
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


# ---------- Pipeline read side (used by dashboard) ----------


def read_signals(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT signal_id, path, source, bucket, title, url, body, date, engagement FROM signals"
    )
    return [
        {
            "signal_id": r[0],
            "path": r[1],
            "source": r[2],
            "bucket": r[3],
            "title": r[4],
            "url": r[5],
            "body": r[6],
            "date": r[7],
            "engagement": r[8],
        }
        for r in cur
    ]


def read_term_stats(conn: sqlite3.Connection, noun_mode: str) -> dict[str, dict]:
    cur = conn.execute(
        "SELECT term, occurrence, engagement_sum, buckets FROM term_stats WHERE noun_mode = ?",
        (noun_mode,),
    )
    return {
        r[0]: {
            "occurrence": r[1],
            "engagement_sum": r[2],
            "buckets": set(r[3].split(",")) if r[3] else set(),
        }
        for r in cur
    }


# ---------- Date-range reads (network over a chosen period) ----------


def _date_clause(date_from: str | None, date_to: str | None, col: str = "s.date") -> tuple[str, list[str]]:
    """SQL fragment (leading ' AND ...' or '') + params for an inclusive date range."""
    sql, params = "", []
    if date_from:
        sql += f" AND {col} >= ?"
        params.append(date_from)
    if date_to:
        sql += f" AND {col} <= ?"
        params.append(date_to)
    return sql, params


def signal_ids_in_range(conn: sqlite3.Connection, date_from: str | None, date_to: str | None) -> set[int]:
    clause, params = _date_clause(date_from, date_to)
    return {r[0] for r in conn.execute(f"SELECT signal_id FROM signals s WHERE 1=1{clause}", params)}


def read_term_stats_in_range(
    conn: sqlite3.Connection, noun_mode: str, date_from: str | None, date_to: str | None
) -> dict[str, dict]:
    """Like ``read_term_stats`` but counted over signals published in the range.
    Also returns per-bucket counts so the dominant bucket needs no extra query."""
    clause, params = _date_clause(date_from, date_to)
    out: dict[str, dict] = {}
    for term, bucket, n, eng in conn.execute(
        f"""
        SELECT st.term, s.bucket, COUNT(*), SUM(s.engagement)
          FROM signal_terms st JOIN signals s USING (signal_id)
         WHERE st.noun_mode = ?{clause}
         GROUP BY st.term, s.bucket
        """,
        [noun_mode, *params],
    ):
        meta = out.setdefault(term, {"occurrence": 0, "engagement_sum": 0.0, "buckets": set(), "bucket_counts": {}})
        meta["occurrence"] += n
        meta["engagement_sum"] += eng or 0.0
        meta["buckets"].add(bucket or "")
        meta["bucket_counts"][bucket or ""] = n
    return out


def read_term_sets_in_range(
    conn: sqlite3.Connection, noun_mode: str, terms: set[str], signal_ids: set[int]
) -> list[set[str]]:
    """One set per signal in ``signal_ids`` (empty sets included, so len() is the
    number of signals in the period), restricted to ``terms``."""
    by_signal: dict[int, set[str]] = {sid: set() for sid in signal_ids}
    if terms:
        placeholders = ",".join("?" * len(terms))
        for sid, term in conn.execute(
            f"SELECT signal_id, term FROM signal_terms WHERE noun_mode = ? AND term IN ({placeholders})",
            [noun_mode, *terms],
        ):
            if sid in by_signal:
                by_signal[sid].add(term)
    return list(by_signal.values())


def read_edges(
    conn: sqlite3.Connection, noun_mode: str, method: str
) -> tuple[dict[frozenset[str], float], dict[frozenset[str], int]]:
    edges: dict[frozenset[str], float] = {}
    counts: dict[frozenset[str], int] = {}
    for a, b, w, c in conn.execute(
        "SELECT term_a, term_b, weight, pair_count FROM edges WHERE noun_mode = ? AND method = ?",
        (noun_mode, method),
    ):
        pair = frozenset((a, b))
        edges[pair] = w
        counts[pair] = c
    return edges, counts


def read_signals_for_term(
    conn: sqlite3.Connection, term: str, noun_mode: str
) -> list[dict]:
    cur = conn.execute(
        """
        SELECT s.signal_id, s.path, s.source, s.bucket, s.title, s.url, s.body, s.date, s.engagement
          FROM signal_terms st
          JOIN signals s ON s.signal_id = st.signal_id
         WHERE st.term = ? AND st.noun_mode = ?
        """,
        (term, noun_mode),
    )
    return [
        {
            "signal_id": r[0],
            "path": r[1],
            "source": r[2],
            "bucket": r[3],
            "title": r[4],
            "url": r[5],
            "body": r[6],
            "date": r[7],
            "engagement": r[8],
        }
        for r in cur
    ]


def read_note_coverage(conn: sqlite3.Connection, term: str) -> dict | None:
    row = conn.execute(
        "SELECT total, rounded, top_titles, fetched_at FROM note_coverage WHERE term = ?",
        (term,),
    ).fetchone()
    if not row:
        return None
    import json as _json
    return {
        "total": row[0],
        "rounded": row[1],
        "top_titles": _json.loads(row[2]) if row[2] else [],
        "fetched_at": row[3],
    }


def read_all_note_coverage(conn: sqlite3.Connection) -> dict[str, dict]:
    import json as _json
    out: dict[str, dict] = {}
    for term, total, rounded, top_titles, fetched_at in conn.execute(
        "SELECT term, total, rounded, top_titles, fetched_at FROM note_coverage"
    ):
        out[term] = {
            "total": total,
            "rounded": rounded,
            "top_titles": _json.loads(top_titles) if top_titles else [],
            "fetched_at": fetched_at,
        }
    return out
