#!/usr/bin/env python3
"""Cross-source correlation analyzer.

Reads all knowledges/*-signals/*.md, extracts terms from titles + body,
scores each term by how many *independent* source buckets it appears in,
and writes a ranked topics report to knowledges/topics/.

Independence model — sources are grouped so that mirror pairs (Zenn/Qiita)
share a bucket. Cross-bucket agreement is stronger evidence of a real
trend than repeated in-bucket agreement.
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import subprocess
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import spacy
from sudachipy import dictionary, tokenizer

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGES_DIR = REPO_ROOT / "knowledges"
OUTPUT_DIR = KNOWLEDGES_DIR / "topics"

SOURCE_BUCKETS: dict[str, str] = {
    "arxiv-signals": "academic",
    "hn-signals": "hn",
    "zenn-signals": "jp-tech",
    "qiita-signals": "jp-tech",
    "bluesky-signals": "jp-social",
    "google-trends-signals": "general-jp",
    "x-signals": "curated",
}

# Terms so common they carry no topic signal.
STOPWORDS_JA = {
    "こと", "もの", "ため", "よう", "ところ", "とき", "場合", "情報", "内容",
    "記事", "説明", "確認", "使用", "利用", "実装", "開発", "作成", "追加",
    "対応", "設定", "機能", "方法", "問題", "話", "点", "処理", "実行",
    "エラー", "サイト", "サービス", "アプリ", "ユーザ", "ユーザー", "アカウント",
    "以下", "以上", "以前", "以後", "全体", "一部", "自分", "みなさん", "皆さん",
    "今回", "今日", "今後", "今週", "今年", "ここ", "そこ", "これ", "それ",
    "ページ", "リンク", "ファイル", "コード", "コマンド", "システム", "データ",
    "画像", "写真", "動画", "文章", "画面", "表示", "作業", "時間", "人",
    "会社", "業務", "運用", "紹介", "書籍", "本", "内",
    "レビュー", "後半", "前半", "章", "節",
}
STOPWORDS_EN = {
    "the", "and", "for", "with", "from", "this", "that", "have", "has",
    "not", "you", "your", "our", "their", "his", "her", "its", "was",
    "are", "were", "will", "would", "can", "could", "should", "may", "might",
    "one", "two", "how", "why", "what", "when", "where", "who", "which",
    "all", "any", "some", "more", "most", "less", "much", "many", "such",
    "new", "get", "use", "using", "used", "make", "made", "let", "like",
    "here", "there", "over", "under", "into", "onto", "out", "off",
    "than", "then", "them", "they", "also", "just", "only",
    "way", "ways", "part", "parts", "thing", "things", "example", "examples",
    "arxiv", "paper", "papers", "post", "posts", "article", "articles",
    "hackernews", "github", "google", "twitter", "note",
    "http", "https", "www", "com", "org", "net", "jp",
    "pdf", "html", "url", "link", "author", "authors", "abs",
    "abstract", "based", "show", "work", "works", "task", "tasks", "model", "models",
    "method", "methods", "result", "results", "study", "studies", "approach",
    "propose", "proposed", "proposes", "novel", "however",
    "these", "those", "each", "both", "very", "well", "even", "still",
    "found", "find", "finding", "findings",
    "through", "across", "about", "against", "along", "around", "before", "after",
    "between", "beyond", "during", "except", "toward", "towards", "within", "without",
    "general", "specific", "different", "similar", "various", "several",
    "large", "small", "high", "low", "long", "short", "hard", "easy",
    "good", "bad", "better", "best", "worse", "worst",
    "real", "true", "false", "given", "including", "compared",
    "world", "system", "field", "space", "case", "role", "term", "level", "state",
    "does", "doing", "done", "goes", "going", "gone", "come", "coming", "came",
    "know", "knew", "known", "think", "thought", "seem", "seems", "seemed",
    "self", "own", "same", "other", "another",
    "provides", "provide", "provided", "designed", "designs", "design",
    "yet", "since", "while", "unless", "though", "although",
    "current", "recent", "future", "past", "present",
    "note", "notes", "review", "reviews",
    "figure", "figures", "fig", "table", "tables",
    "input", "inputs", "output", "outputs",
    "cnn", "rnn",
    "top", "bottom", "left", "right",
    "must", "might", "shall",
    "large-scale", "small-scale",
    "our", "we", "us",
    "live",
    "news", "related", "now", "via", "per", "yet", "already",
    "however", "moreover", "furthermore", "additionally",
    "yahoo", "nikkansports",
    "first", "second", "third", "fourth", "fifth", "last", "next",
    "until", "since", "again", "always", "never", "often", "usually", "sometimes",
    "years", "year", "days", "day", "months", "month", "weeks", "week",
    "hours", "hour", "minutes", "minute", "seconds", "second",
    "post", "posted", "posts",
}

# Verbs so common they carry no topical signal.
STOPWORDS_VERBS_JA = {
    "為る", "する", "有る", "在る", "居る", "成る", "為さる", "行う", "使う", "見る",
    "思う", "考える", "示す", "得る", "できる", "出来る", "持つ", "取る", "作る",
    "書く", "読む", "言う", "話す", "聞く", "知る", "分かる", "解る",
    "始まる", "終わる", "続く", "続ける", "含む", "含まれる", "含める",
    "示せる", "示される", "行える", "行われる", "選ぶ", "選ぶれる",
    "呼ぶ", "呼ばれる", "決める", "決まる",
    "受ける", "与える", "つく", "付く", "付ける",
    "出す", "出る", "入れる", "入る",
    "上げる", "下げる",
    "見せる", "見える",
    "書ける", "書かれる",
    "読む", "読める",
}
STOPWORDS_VERBS_EN = {
    "be", "have", "do", "get", "make", "take", "use", "used",
    "give", "put", "let", "keep", "hold", "run", "come", "go",
    "say", "tell", "see", "look", "find", "found", "think", "know",
    "want", "need", "try", "seem", "become", "show", "shown", "consider",
    "provide", "provided", "proposed", "propose", "propose",
    "introduce", "present", "demonstrate", "demonstrate",
    "achieve", "obtain", "evaluate", "compare", "based", "base",
    "include", "contain", "involve", "require",
    "allow", "enable", "support", "improve", "improve",
    "extend", "learn", "generate", "produce", "yield",
    "return", "start", "end", "continue", "follow",
    "cause", "help", "work", "call",
    "study", "explore", "investigate", "examine",
    "focus", "aim", "point", "note", "notice",
    "exist", "remain", "address", "reduce", "combine",
    "preserve", "share", "build", "identify", "match",
    "fix", "establish", "apply", "outperform", "ground",
    "represent", "measure", "compute", "compute",
    "map", "map", "assess", "characterize",
    "leverage", "handle", "process", "define",
    "adapt", "adopt", "refine", "select", "estimate",
}


# Katakana transliterations of English function/common words that Sudachi's
# normalizer emits when it tokenizes an English abstract. We treat these as
# stopwords too, because otherwise arXiv abstracts drag them to the top.
STOPWORDS_KATA = {
    "ポスト", "オープン", "テーマ", "バージョン", "アンド", "フォー", "オブ",
    "アップ", "ダウン", "アウト", "イン", "オン", "オフ", "アット", "ウィズ",
    "ウェブ", "ケース", "テスト", "モデル", "データ", "コード", "ページ",
}


def _strip_urls_and_paths(text: str) -> str:
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"`[^`]*`", " ", text)  # inline code / paths
    return text


EN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.#-]{2,}")


@dataclass
class Signal:
    path: Path
    bucket: str
    source: str
    title: str
    body: str
    engagement: float
    date: str
    url: str = ""


@dataclass
class TermStats:
    buckets: set[str] = field(default_factory=set)
    signals: list[Signal] = field(default_factory=list)
    count: int = 0


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fm_body = text[4:end]
    rest = text[end + 5 :]
    fm: dict = {}
    for line in fm_body.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        try:
            fm[key] = json.loads(value)
        except json.JSONDecodeError:
            fm[key] = value
    return fm, rest


def engagement_score(fm: dict, source: str) -> float:
    # Rough normalization across sources so ranking downstream is stable.
    if source == "hackernews":
        return float(fm.get("points") or 0) + 3 * float(fm.get("num_comments") or 0)
    if source == "arxiv":
        return 10.0  # arXiv has no engagement metric; treat as baseline weight
    if source == "zenn":
        return float(fm.get("liked_count") or 0) + float(fm.get("bookmarked_count") or 0)
    if source == "qiita":
        return float(fm.get("likes_count") or 0) + float(fm.get("stocks_count") or 0)
    if source == "bluesky":
        return (
            float(fm.get("like_count") or 0)
            + 2 * float(fm.get("repost_count") or 0)
            + float(fm.get("reply_count") or 0)
        )
    if source == "google_trends":
        traffic = str(fm.get("approx_traffic") or "").rstrip("+")
        try:
            return float(traffic)
        except ValueError:
            return 100.0
    return 5.0


METADATA_BULLET_RE = re.compile(r"^-\s+[A-Za-z][\w ]*:\s")


def strip_metadata_bullets(body: str) -> str:
    """Drop rendered metadata bullet lines while keeping headings and prose.

    Used for tokenization so counts of "comments", "length", "author" don't
    dominate the index. Preserves ``## Abstract``, ``## Story text``, and any
    narrative body that appears after bullets.
    """
    return "\n".join(
        line for line in body.splitlines() if not METADATA_BULLET_RE.match(line.strip())
    )


def content_for_display(body: str) -> str:
    """Return the body cleaned only of metadata bullets, keeping everything else.

    Used by the dashboard so cards can show the actual abstract / post text.
    """
    return strip_metadata_bullets(body).strip()


# Source-format boilerplate that would otherwise be tokenized as topic terms
# (outlet credits → "Yahoo!ニュース", "日刊スポーツ"; Raindrop template →
# "raindrop"; X page titles → "Xユーザー" + account names). Only the text fed
# to term extraction is cleaned; display text keeps outlet names because they
# help when reading a signal or writing a briefing.
_TEMPLATE_HEADINGS = {"## Related news", "## Note (from Raindrop)", "## Excerpt"}
# Google Trends: "- [headline（publisher）](url) — aggregator"
_TRENDS_NEWS_LINE_RE = re.compile(r"^-\s+\[(?P<headline>.*)\]\([^)]*\)(?:\s+—\s+.*)?$")
# Japanese news credits the publisher in a trailing full-width parenthetical.
_TRAILING_CREDIT_RE = re.compile(r"\s*（[^（）]{1,25}）\s*$")
# X page title: "(3) Xユーザーのconnect24hさん: 「...」 / X"
_X_TITLE_PREFIX_RE = re.compile(r"^(#\s+)?(?:\(\d+\)\s*)?Xユーザーの.+?さん[:：]\s*")
_X_TITLE_SUFFIX_RE = re.compile(r"\s*/\s*X\s*$")


def term_text(signal: "Signal") -> str:
    """Title + body with source boilerplate removed, for term extraction."""
    lines: list[str] = []
    for line in f"{signal.title}\n{signal.body}".splitlines():
        stripped = line.strip()
        if stripped in _TEMPLATE_HEADINGS:
            continue
        if signal.source == "google_trends":
            m = _TRENDS_NEWS_LINE_RE.match(stripped)
            if m:
                line = _TRAILING_CREDIT_RE.sub("", m["headline"])
        elif signal.source == "x":
            line = _X_TITLE_SUFFIX_RE.sub("", _X_TITLE_PREFIX_RE.sub(r"\1", line))
        lines.append(line)
    return "\n".join(lines)


def signal_date(fm: dict) -> str:
    for key in ("created", "created_at", "published", "published_at", "date"):
        value = fm.get(key)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return ""


def load_signals() -> list[Signal]:
    signals: list[Signal] = []
    for sub in KNOWLEDGES_DIR.iterdir():
        if not sub.is_dir() or sub.name not in SOURCE_BUCKETS:
            continue
        bucket = SOURCE_BUCKETS[sub.name]
        for path in sub.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            source = fm.get("source") or sub.name.replace("-signals", "")
            title = str(fm.get("title") or "")
            content = content_for_display(body)
            # Prefer canonical source URL; fall back to type-specific alternates.
            url = str(
                fm.get("url")
                or fm.get("abs_url")
                or fm.get("hn_url")
                or ""
            )
            signals.append(
                Signal(
                    path=path,
                    bucket=bucket,
                    source=source,
                    title=title,
                    body=content,
                    engagement=engagement_score(fm, source),
                    date=signal_date(fm),
                    url=url,
                )
            )
    return signals


NounMode = str  # "all" (NOUN+PROPN / 普通名詞+固有名詞) | "propn" (PROPN / 固有名詞 only)


# Sentence / line boundaries for slot windows. "." only counts when followed by
# whitespace so "gpt-5.6" and "u.s" stay intact.
_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？!?\n]|\.(?=\s)")


@dataclass(frozen=True)
class Slot:
    off: int                # char offset in the URL-stripped text
    sent: int               # sentence index
    kind: str               # "n" (noun run) or "v" (verb)
    units: tuple[str, ...]  # noun: member tokens (+ compound); verb: (lemma,)


class TermExtractor:
    """Extract topic-bearing nouns (English via spaCy, Japanese via Sudachi).

    ``noun_mode`` controls the POS filter:
      - "all"   : NOUN + PROPN in EN; 普通名詞 + 固有名詞 in JA (default)
      - "propn" : PROPN only in EN; 固有名詞 only in JA — surfaces named
                  entities (products, model names, people, orgs) and tends
                  to yield sharper topic clusters.

    Adjacent noun tokens are joined into compound phrases ("prompt injection",
    "エージェント設計").
    """

    def __init__(self, noun_mode: NounMode = "all") -> None:
        if noun_mode not in {"all", "propn"}:
            raise ValueError(f"unknown noun_mode: {noun_mode}")
        self.noun_mode = noun_mode
        self._tok = dictionary.Dictionary().create()
        self._mode = tokenizer.Tokenizer.SplitMode.C
        # Both attribute_ruler and lemmatizer must stay: without attribute_ruler
        # tok.pos_ is empty; without lemmatizer verb extraction returns surface
        # forms like "generates" instead of "generate".
        self._nlp = spacy.load(
            "en_core_web_sm",
            disable=["parser", "ner"],
        )

    # Each noun "slot" is one contiguous noun run at a char offset: its member
    # tokens plus, for runs of 2+, the joined compound. Flattening the slots in
    # order reproduces the legacy extract() output exactly.

    def _english_noun_slots(self, doc) -> list[tuple[int, list[str]]]:
        accepted_pos = {"PROPN"} if self.noun_mode == "propn" else {"NOUN", "PROPN"}
        slots: list[tuple[int, list[str]]] = []
        run: list[str] = []
        run_start = 0

        def close_run() -> None:
            if run:
                units = list(run)
                if len(run) >= 2:
                    units.append(" ".join(run))
                slots.append((run_start, units))
            run.clear()

        for tok in doc:
            surface = tok.text
            # Only interested in ASCII noun tokens; JP text is handled by Sudachi.
            if not surface.isascii() or not surface[0].isalpha():
                close_run()
                continue
            is_noun = tok.pos_ in accepted_pos
            token = surface.lower().strip(".,;:!?()[]\"'`")
            if not is_noun or len(token) < 3 or token in STOPWORDS_EN:
                close_run()
                continue
            if not run:
                run_start = tok.idx
            run.append(token)
        close_run()
        return slots

    def _japanese_noun_slots(self, morphemes) -> list[tuple[int, list[str]]]:
        accepted_subpos = {"固有名詞"} if self.noun_mode == "propn" else {"普通名詞", "固有名詞"}
        slots: list[tuple[int, list[str]]] = []
        run: list[str] = []
        run_start = 0

        def close_run() -> None:
            if run:
                units = list(run)
                if len(run) >= 2:
                    units.append("".join(run))
                slots.append((run_start, units))
            run.clear()

        for morpheme in morphemes:
            pos = morpheme.part_of_speech()
            surface_raw = morpheme.surface()
            normalized = morpheme.normalized_form()
            # Skip ASCII surface tokens; the English branch already handled them.
            if re.fullmatch(r"[A-Za-z0-9+.#-]+", surface_raw):
                close_run()
                continue
            is_noun = pos[0] == "名詞" and pos[1] in accepted_subpos
            if is_noun and len(normalized) >= 2:
                if normalized in STOPWORDS_JA or normalized in STOPWORDS_KATA:
                    close_run()
                    continue
                if not run:
                    run_start = morpheme.begin()
                run.append(normalized)
            else:
                close_run()
        close_run()
        return slots

    def _english_verbs(self, doc) -> list[tuple[int, str]]:
        verbs: list[tuple[int, str]] = []
        for tok in doc:
            if tok.pos_ != "VERB":
                continue
            lemma = (tok.lemma_ or tok.text).lower()
            if not lemma or not lemma.isascii():
                continue
            if lemma in STOPWORDS_VERBS_EN or len(lemma) < 3:
                continue
            verbs.append((tok.idx, lemma))
        return verbs

    def _japanese_verbs(self, morphemes) -> list[tuple[int, str]]:
        # JP has two verb-carriers: pure 動詞 (native verbs like 続く, 使う) and
        # 名詞/サ変可能 (Sino-Japanese verbal nouns like 開催, 鑑賞, 展示 —
        # meaningful action words even though POS is noun).
        verbs: list[tuple[int, str]] = []
        for morpheme in morphemes:
            pos = morpheme.part_of_speech()
            is_verb = pos[0] == "動詞"
            is_sahen_noun = pos[0] == "名詞" and pos[1] == "普通名詞" and pos[2] == "サ変可能"
            if not (is_verb or is_sahen_noun):
                continue
            surface_raw = morpheme.surface()
            # Sudachi's normalized_form katakanizes ASCII words (propose → プロポーズ);
            # skip anything whose surface is ASCII — those went through spaCy.
            if re.fullmatch(r"[A-Za-z0-9+.#-]+", surface_raw or ""):
                continue
            lemma = morpheme.normalized_form()
            if not lemma or lemma in STOPWORDS_VERBS_JA or lemma in STOPWORDS_JA:
                continue
            if len(lemma) < 2:
                continue
            verbs.append((morpheme.begin(), lemma))
        return verbs

    def _parse(self, text: str):
        try:
            doc = self._nlp(text)
        except Exception:
            doc = []
        try:
            morphemes = list(self._tok.tokenize(text, self._mode))
        except Exception:
            morphemes = []
        return doc, morphemes

    def extract(self, text: str) -> list[str]:
        text = _strip_urls_and_paths(text)
        doc, morphemes = self._parse(text)
        slots = self._english_noun_slots(doc) + self._japanese_noun_slots(morphemes)
        return [u for _, units in slots for u in units]

    def extract_verbs(self, text: str) -> list[str]:
        """Return lemma-form verbs from the text (JA+EN), stopwords removed."""
        text = _strip_urls_and_paths(text)
        doc, morphemes = self._parse(text)
        return [v for _, v in self._english_verbs(doc) + self._japanese_verbs(morphemes)]

    def extract_slots(self, text: str) -> list[Slot]:
        """Positioned noun runs and verbs, sorted by char offset.

        Used by the buffer-overlap network, which needs to know which nouns /
        verbs sit near each proper-noun mention. ``sent`` is a sentence index so
        windows never cross a sentence (or line) boundary.
        """
        text = _strip_urls_and_paths(text)
        doc, morphemes = self._parse(text)
        bounds = [m.start() for m in _SENTENCE_BOUNDARY_RE.finditer(text)]
        slots: list[Slot] = []
        for off, units in self._english_noun_slots(doc) + self._japanese_noun_slots(morphemes):
            slots.append(Slot(off, bisect.bisect_right(bounds, off), "n", tuple(units)))
        for off, verb in self._english_verbs(doc) + self._japanese_verbs(morphemes):
            slots.append(Slot(off, bisect.bisect_right(bounds, off), "v", (verb,)))
        slots.sort(key=lambda s: (s.off, s.kind))
        return slots


def build_index_and_term_sets(
    signals: list[Signal],
    noun_mode: NounMode = "all",
) -> tuple[dict[str, TermStats], list[set[str]]]:
    """Single tokenization pass returning both the inverted index and per-signal
    term sets. Prefer this to calling ``build_index`` and ``signal_term_sets``
    separately.
    """
    extractor = TermExtractor(noun_mode=noun_mode)
    index: dict[str, TermStats] = defaultdict(TermStats)
    term_sets: list[set[str]] = []
    for signal in signals:
        text = term_text(signal)
        terms: set[str] = set(extractor.extract(text))
        term_sets.append(terms)
        for term in terms:
            stats = index[term]
            stats.buckets.add(signal.bucket)
            stats.signals.append(signal)
            stats.count += 1
    return dict(index), term_sets


def build_index(signals: list[Signal], noun_mode: NounMode = "all") -> dict[str, TermStats]:
    """Legacy shim. Prefer ``build_index_and_term_sets``."""
    index, _ = build_index_and_term_sets(signals, noun_mode=noun_mode)
    return index


def rank_terms(index: dict[str, TermStats], min_buckets: int = 2) -> list[tuple[str, TermStats]]:
    candidates = [
        (term, stats)
        for term, stats in index.items()
        if len(stats.buckets) >= min_buckets
    ]
    # Rank primarily by cross-bucket agreement, then by summed engagement
    # (raw count is noisy — arXiv abstracts inflate it).
    candidates.sort(
        key=lambda pair: (
            len(pair[1].buckets),
            sum(s.engagement for s in pair[1].signals),
            pair[1].count,
        ),
        reverse=True,
    )
    return candidates


def signal_term_sets(
    signals: list[Signal],
    filter_terms: set[str] | None = None,
    noun_mode: NounMode = "all",
) -> list[set[str]]:
    """Return one set of distinct terms per signal.

    Terms come from the same extractor (with EN/JA/KATA stopwords already
    applied). If ``filter_terms`` is given, restrict to that set.
    """
    extractor = TermExtractor(noun_mode=noun_mode)
    out: list[set[str]] = []
    for signal in signals:
        text = term_text(signal)
        terms = set(extractor.extract(text))
        if filter_terms is not None:
            terms &= filter_terms
        out.append(terms)
    return out


def _pair_and_term_counts(
    term_sets: list[set[str]],
) -> tuple[Counter, Counter]:
    term_counts: Counter = Counter()
    pair_counts: Counter = Counter()
    for terms in term_sets:
        term_counts.update(terms)
        ordered = sorted(terms)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                pair_counts[frozenset((ordered[i], ordered[j]))] += 1
    return pair_counts, term_counts


EdgeMethod = str  # "npmi" | "pmi" | "jaccard" | "count"


def edge_weights(
    term_sets: list[set[str]],
    method: EdgeMethod = "npmi",
    min_pair_count: int = 2,
) -> tuple[dict[frozenset[str], float], dict[str, int]]:
    """Compute an edge weight for every co-occurring pair.

    Supported methods:
      - "count"   : raw co-occurrence count (>= min_pair_count)
      - "jaccard" : |A ∩ B| / |A ∪ B|            (in [0, 1])
      - "pmi"     : log( p(a,b) / (p(a)p(b)) )    (unbounded)
      - "npmi"    : PMI / -log(p(a,b))            (in [-1, 1])

    Returns (edges, term_counts). ``term_counts[t]`` is the number of signals
    that contain term ``t`` (= the node occurrence count).
    """
    import math

    pair_counts, term_counts = _pair_and_term_counts(term_sets)
    total = len(term_sets)
    if total == 0:
        return {}, {}

    edges: dict[frozenset[str], float] = {}
    for pair, c_xy in pair_counts.items():
        if c_xy < min_pair_count:
            continue
        x, y = tuple(pair)
        c_x = term_counts[x]
        c_y = term_counts[y]
        if method == "count":
            edges[pair] = float(c_xy)
            continue
        if method == "jaccard":
            union = c_x + c_y - c_xy
            edges[pair] = c_xy / union if union > 0 else 0.0
            continue
        # PMI / NPMI branches
        p_x = c_x / total
        p_y = c_y / total
        p_xy = c_xy / total
        try:
            pmi = math.log(p_xy / (p_x * p_y))
        except ValueError:
            continue
        if method == "pmi":
            edges[pair] = pmi
        elif method == "npmi":
            denom = -math.log(p_xy)
            edges[pair] = pmi / denom if denom > 0 else 1.0
        else:
            raise ValueError(f"unknown method: {method}")
    return edges, dict(term_counts)


def neighborhood(
    term: str,
    edges: dict[frozenset[str], float],
    k: int = 15,
    min_weight: float = float("-inf"),
) -> list[tuple[str, float]]:
    """Top-k neighbors of ``term`` by edge weight (descending)."""
    neighbors: list[tuple[str, float]] = []
    for pair, w in edges.items():
        if term in pair and w >= min_weight:
            other = next(iter(pair - {term}))
            neighbors.append((other, w))
    neighbors.sort(key=lambda x: x[1], reverse=True)
    return neighbors[:k]


# Back-compat shim; deprecated in favor of edge_weights(method="npmi").
def compute_npmi(
    term_sets: list[set[str]],
    min_pair_count: int = 2,
) -> tuple[dict[frozenset[str], float], dict[str, int]]:
    return edge_weights(term_sets, method="npmi", min_pair_count=min_pair_count)


def opportunity_factor(coverage_total: int | None) -> float:
    """Multiplier that rewards under-covered topics.

    note.com caps `total_count` at 10000; treat that as saturation.
    """
    if coverage_total is None:
        return 1.0  # unknown -> neutral
    if coverage_total == 0:
        return 3.0
    if coverage_total < 10:
        return 2.0
    if coverage_total < 100:
        return 1.5
    if coverage_total < 1000:
        return 1.0
    if coverage_total < 10000:
        return 0.5
    return 0.3  # 10000 cap -> heavily discounted


def recommendation_score(stats, coverage: dict | None) -> float:
    """Score a term for likely-good-to-write-about-ness.

    Works with both TermStats (has ``.signals``) and DB-backed proxies that
    expose ``.engagement_sum`` directly. Callers should provide whichever the
    context has.
    """
    import math

    buckets = len(stats.buckets)
    engagement_sum = getattr(stats, "engagement_sum", None)
    if engagement_sum is None:
        engagement_sum = sum(s.engagement for s in stats.signals)
    coverage_total = coverage.get("total") if coverage else None
    return (
        buckets
        * math.log1p(engagement_sum)
        * opportunity_factor(coverage_total)
    )


def differentiation_hint(coverage_total: int | None) -> str:
    if coverage_total is None:
        return "note.comカバレッジ未取得 — 判断保留"
    if coverage_total == 0:
        return "**未開拓トピック**。第一人者ポジションを取れる可能性。基礎解説から入って市場を作る戦略。"
    if coverage_total < 10:
        return "**希薄**。参入余地が大きい。競合が少ないので基礎解説 + 実例で十分差別化できる。"
    if coverage_total < 100:
        return "**中密度**。既存記事より深さ・実測・具体的事例で優位を作る必要。"
    if coverage_total < 1000:
        return "**やや飽和**。切り口の差別化必須。比較検証・失敗事例・運用コストなど、入門記事にはない角度を狙う。"
    return "**飽和気味**。「使ってみた」系は避け、**落とし穴・限界・アンチパターン・長期運用コスト・既存代替との比較** など、書き手の実体験を要する角度で勝負。"


def note_coverage(term: str) -> dict | None:
    """Query note.com for the paid-note coverage of a term.

    Returns {total, rounded, top_titles} or None on failure. total_count is
    capped at 10000 by note.com; rounded_total_count is the display value.
    """
    url = (
        "https://note.com/api/v3/searches?"
        + urllib.parse.urlencode(
            {"context": "note", "q": term, "mode": "paid", "size": 5}
        )
    )
    try:
        result = subprocess.run(
            ["curl", "-sS", "--fail-with-body", "-A", "note-topix/0.1", url],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    notes = (payload.get("data") or {}).get("notes") or {}
    contents = notes.get("contents") or []
    return {
        "total": notes.get("total_count"),
        "rounded": notes.get("rounded_total_count"),
        "top_titles": [c.get("name") or "(no title)" for c in contents[:3]],
    }


def render_report(
    ranked: list[tuple[str, TermStats]], total_signals: int, coverage: dict[str, dict | None]
) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# 横断相関レポート — {today}",
        "",
        f"- 対象シグナル総数: {total_signals}",
        f"- 独立バケット: {sorted(set(SOURCE_BUCKETS.values()))}",
        f"- 掲載条件: 2バケット以上で言及されたターム",
        f"- 上位: {min(len(ranked), 50)} 件",
        "",
        "スコアは (独立バケット数, 総出現数, エンゲージメント合計) の辞書順。",
        "各ターム下のシグナルは、エンゲージメント上位3件のみ抜粋。",
        "",
        "---",
        "",
    ]
    for rank, (term, stats) in enumerate(ranked[:50], start=1):
        top_signals = sorted(stats.signals, key=lambda s: s.engagement, reverse=True)[:3]
        lines.append(
            f"## {rank}. `{term}` "
            f"— buckets={len(stats.buckets)} ({', '.join(sorted(stats.buckets))}), "
            f"count={stats.count}"
        )
        lines.append("")
        cov = coverage.get(term)
        if cov is not None:
            total = cov.get("total")
            rounded = cov.get("rounded")
            titles = cov.get("top_titles") or []
            if total is None:
                cov_summary = "note.com paid: (query failed)"
            elif total == 0:
                cov_summary = "**note.com paid: 0** ← 未開拓"
            elif total < 10:
                cov_summary = f"note.com paid: {rounded or total} ← 希薄"
            elif total < 100:
                cov_summary = f"note.com paid: {rounded or total}"
            else:
                cov_summary = f"note.com paid: {rounded or total} ← 飽和気味"
            lines.append(f"- {cov_summary}")
            if titles:
                lines.append(f"  - 既存タイトル例: {' / '.join(titles)}")
        lines.append("")
        lines.append("シグナル上位:")
        for signal in top_signals:
            rel = signal.path.relative_to(REPO_ROOT)
            title = signal.title.strip() or "(no title)"
            lines.append(
                f"- [{signal.source} · eng={signal.engagement:g}] {title} — `{rel}`"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-note-coverage",
        action="store_true",
        help="Skip the note.com paid-coverage lookup (offline / faster runs).",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    signals = load_signals()
    if not signals:
        print("no signals found; run the fetch_* scripts first", file=sys.stderr)
        return 1
    index = build_index(signals)
    ranked = rank_terms(index)

    coverage: dict[str, dict | None] = {}
    if not args.no_note_coverage:
        for term, _ in ranked[:50]:
            coverage[term] = note_coverage(term)
            time.sleep(0.5)  # be polite to note.com

    report = render_report(ranked, len(signals), coverage)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = OUTPUT_DIR / f"{today}-correlations.md"
    out.write_text(report, encoding="utf-8")
    print(
        f"done: {len(signals)} signals, "
        f"{len(index)} terms indexed, "
        f"{len(ranked)} cross-bucket -> {out.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
