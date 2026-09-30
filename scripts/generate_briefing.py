#!/usr/bin/env python3
"""Briefing generator for note.com free-article drafting.

Given a topic (a community name, a single keyword, or auto-picked), assemble
all relevant signals + note.com coverage + community context into a bundle,
and produce a ready-to-copy prompt for a Claude subagent. The subagent then
drafts a briefing markdown file to ``documents/briefings/``.

The briefing is *not* a finished article — it is a research/thinking scaffold
that leaves the author's opinion, voice, and personal experience unwritten
(that's what makes the article yours and not Claude's).

Subcommands:

  suggest    — list candidate topics from the DB with heuristic scores
  prepare    — bundle a chosen topic + write bundle.json and prompt.md
  import     — move a subagent-produced markdown into documents/briefings/
  auto       — unattended: for each named community of the buffer-overlap
               network (UI default view) that has no briefing yet, generate
               one via ``claude -p`` and write it to documents/briefings/.
               Run from run_pipeline.py; see cmd_auto for the skip rules.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
BRIEFINGS_DIR = REPO_ROOT / "documents" / "briefings"
TMP_DIR = Path("/tmp/note-topix-briefings")

sys.path.insert(0, str(SCRIPTS_DIR))
from buffer_edges import DEFAULT_VIEW  # noqa: E402
from llm import call_claude, strip_fence  # noqa: E402
from term_db import (  # noqa: E402
    connect,
    get_aliases,
    get_classifications,
    jaccard,
    list_briefing_log,
    list_community_names,
    read_community_name,
    save_briefing_log,
    read_all_note_coverage,
    read_signals_for_term,
    read_term_stats,
    read_verbs_for_signals,
    signals_containing_terms,
)


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s-]+", "-", text).strip("-")
    return text.lower()[:60] or "topic"


def resolve_topic_terms(
    conn,
    community_name: str | None,
    term: str | None,
    terms_csv: str | None,
    noun_mode: str,
) -> tuple[str, list[str], dict | None]:
    """Return (topic_label, term_list, community_info_or_None)."""
    if community_name:
        for com in list_community_names(conn):
            if com["name"] == community_name:
                return community_name, com.get("members") or [], com
        raise SystemExit(f"community not found: {community_name}")

    if terms_csv:
        term_list = [t.strip() for t in terms_csv.split(",") if t.strip()]
        return " / ".join(term_list[:3]), term_list, None

    if term:
        # Single-term topic: use the term itself as the "cluster".
        return term, [term], None

    raise SystemExit("must supply --community, --term, or --terms")


def collect_bundle(
    conn,
    topic_label: str,
    term_list: list[str],
    community_info: dict | None,
    noun_mode: str,
    max_signals: int = 25,
) -> dict:
    # All signals that contain any of the topic terms
    sig_ids = signals_containing_terms(conn, term_list, noun_mode)

    # Load full signal records for those ids, ranked by engagement
    signals: list[dict] = []
    if sig_ids:
        placeholders = ",".join("?" * len(sig_ids))
        for row in conn.execute(
            f"""
            SELECT source, bucket, title, url, body, date, engagement
              FROM signals
             WHERE signal_id IN ({placeholders})
             ORDER BY engagement DESC
            """,
            sig_ids,
        ):
            signals.append(
                {
                    "source": row[0],
                    "bucket": row[1],
                    "title": row[2] or "",
                    "url": row[3] or "",
                    "body": row[4] or "",
                    "date": row[5] or "",
                    "engagement": row[6],
                }
            )

    # Verbs that co-occur with the topic terms
    verb_counts = read_verbs_for_signals(conn, sig_ids)
    top_verbs = sorted(verb_counts.items(), key=lambda x: -x[1])[:25]

    # note.com paid-note coverage for each topic term
    coverage_data: dict[str, dict] = {}
    all_cov = read_all_note_coverage(conn)
    for t in term_list[:10]:
        if t in all_cov:
            coverage_data[t] = all_cov[t]

    # LLM-classification labels for topic terms
    class_map = get_classifications(conn, term_list)

    return {
        "topic_label": topic_label,
        "community": community_info,
        "terms": term_list,
        "term_classifications": class_map,
        "top_verbs": top_verbs,
        "signals": signals[:max_signals],
        "signal_total": len(signals),
        "note_coverage": coverage_data,
    }


PROMPT_TEMPLATE = """あなたは、note.com で無料記事を執筆しようとしている書き手の
リサーチアシスタントです。書き手は note を始めたばかりで、まだ記事はありません。
戦略として **速報系ニュースへの独自視点コメント** を積み重ねてキャラを醸成し、
将来的にその読者を有料記事へ導きます。

つまり、この briefing の目的は **完成した記事を書くこと** ではありません。
書き手が **自分の視点・経験・意見** を注ぎ込めるように、**事実の整理・複数の論点・
問いかけ・書き出しと締めのたたき台** を提供することです。

**Claude 自身が意見を書き切ってはいけません**。「私はこう思う」の一人称は書き手のもの。
Claude は事実整理と論点提示にとどめてください。

---

# 入力バンドル

以下は今回の briefing 対象トピックのリサーチ資料です。

```json
{bundle_json}
```

---

# 出力仕様

{output_instruction}

```markdown
---
topic: {topic_label}
date: <today ISO>
signal_count: {signal_count}
terms: {terms_json}
---

# {topic_label}

## 何が起きているか (300-500字)

事実を客観的に。複数ソースが何と報じ、何が新しいのか。数字や固有名詞を引用。

## 主な論点 (3-5個、各150-250字)

このトピックには何通りの見方があるか。楽観的視点、懸念、業界構造への影響、技術的
興味、社会的インパクトなど、切り口を意識的にバラす。各論点は独立した見解の提示に
とどめる (書き手が「私はこの論点でこう考える」と選べる状態)。

- **論点A: <一言>** — <本文>
- **論点B: <一言>** — <本文>
- ...

## 引用可能な事実・数字

- 数字X (出典: <signal URL>)
- 事例Y (出典: <signal URL>)
- ...

## なぜ〇〇なのか (記事の核にできる疑問, 3個)

書き手は研究者・エンジニアで、「仕組みを噛み砕いて説明する」記事を得意としている。
そこで、**調べれば答えが出る「なぜ」の疑問**を 3個立てる。1個ずつ次の形式で書く。

- **なぜ〜なのか** — そのまま記事タイトルになる問い。固有名詞を含め、具体的に
  (悪い例:「なぜAIは進化するのか」/ 良い例:「なぜKVキャッシュの圧縮が、パラメータ削減より効くのか」)
  - **引っかかりどころ**: 直感に反する点、一見すると矛盾している点を1-2文で。読者が
    「確かに言われてみれば」と思う所を突く
  - **調べる手がかり**: 答えに辿り着くために確認すべき論文・数字・仕組み。バンドル内の
    signals から根拠になりそうなものを挙げる。不足なら何を調べればよいかを書く

条件:
- 意見や好みではなく、**仕組み・原理・トレードオフの説明で答えが出る**問いにする
- 「どう思うか」「今後どうなるか」のような予想を問うものは入れない
- 3個は別々の論点から取る。同じ話の言い換えにしない

## あなた（書き手）に問いたい

書き手が自分の視点を引き出せるように、経験や判断を尋ねる問いを 5個。抽象的すぎず、
書き手の実務や思考体験に紐付けやすいものを。

1. <問い1>
2. <問い2>
3. ...

## 書き出しのたたき台 (100-150字, 自由改変前提)

読者の関心を引く導入。ただし書き手のキャラが乗る余地を残す (「以前○○を経験したとき…」
「これを聞いたとき、まず思ったのは…」など、書き手が続きを書きやすい形の文で終える)。

## 締めのたたき台 (100-150字, 自由改変前提)

議論を閉じすぎず、書き手の次の行動や問いに繋げる形で。

## note.com 既存記事の傾向

note.com に同トピックで既に書かれている代表タイトル (`note_coverage.top_titles`) を
見て、既に書かれている角度と、書き手が差別化できそうな角度を1-2行でメモ。
```

---

# 大事なガイダンス

- **書き手の一人称は書かない**。「私は」「筆者は」等の主観記述はゼロで。
- **意見を断定しない**。「〜と考えられる」「〜という見方もある」で提示する。
- **具体を優先**。抽象語を並べるより、数字・固有名詞・実例を優先。
- 「なぜ〇〇なのか」は記事の柱になる部分。**そのまま記事タイトルとして成立する具体性**で書く。
- **note.com 既存記事の傾向** から、書き手の切り口候補を明示的に浮上させる。
- `community.buffers` がある場合、それはメンバー同士を「似た文脈で語られている」と
  結びつけている共通の名詞・動詞。論点を立てる際の切り口の手がかりにする。
- {delivery_rule}

"""


def render_prompt(bundle: dict, output_path: Path | None = None) -> str:
    """``output_path`` set: the agent saves the file itself (subagent flow).
    None: the agent prints the markdown and the caller saves it (claude -p)."""
    if output_path is not None:
        output_instruction = f"以下の Markdown を書き出してください。ファイルパス: `{output_path}`"
        delivery_rule = "出力は上記テンプレのみ。前置きや後書きは書かない。Write ツールでファイルに直接保存。"
    else:
        output_instruction = "以下の Markdown だけを出力してください (ファイル保存は呼び出し側で行います)。"
        delivery_rule = "出力は上記テンプレの Markdown 本文のみ。前置き・後書き・コードフェンスは書かない。"
    return PROMPT_TEMPLATE.format(
        bundle_json=json.dumps(bundle, ensure_ascii=False, indent=2),
        output_instruction=output_instruction,
        delivery_rule=delivery_rule,
        topic_label=bundle["topic_label"],
        signal_count=bundle["signal_total"],
        terms_json=json.dumps(bundle["terms"], ensure_ascii=False),
    ).replace("<today ISO>", datetime.now().strftime("%Y-%m-%d"))  # local date, like the filename


def _undertaught(members: list[str], all_cov: dict) -> float:
    """Fraction of (covered) member terms with < 100 paid notes on note.com."""
    counted = [all_cov[m] for m in members[:10] if m in all_cov]
    if not counted:
        return 0.0
    return sum(1 for c in counted if (c.get("total") or 0) < 100) / len(counted)


def cmd_auto(args: argparse.Namespace) -> int:
    """Generate briefings for newly formed communities.

    Skip rules (to keep a 6-hourly cron cheap and the briefings folder sane):
      - unnamed communities (run ``name_communities.py run`` first)
      - communities already briefed (same member hash), even if the file was
        deleted since — deletion means "not interested"
      - communities that are a slight drift (member Jaccard >= --similar) of one
        briefed within the last --cooldown-days
    Remaining ones are ranked like ``suggest`` (signals x undertaught) and at
    most --max are generated per run.
    """
    from name_communities import build_buffer_bundles  # heavy import, only here

    conn = connect()
    communities = build_buffer_bundles(conn, DEFAULT_VIEW, min_size=args.min_size)
    log = list_briefing_log(conn)
    logged = {entry["members_hash"] for entry in log}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.cooldown_days)).isoformat()
    recent = [entry for entry in log if entry["created_at"] >= cutoff]
    aliases = get_aliases(conn)
    all_cov = read_all_note_coverage(conn)

    candidates: list[tuple[float, dict, dict]] = []
    for com in communities:
        name = read_community_name(conn, com["members_hash"])
        if not name:
            print(f"  skip #{com['community_id']} (unnamed)")
            continue
        if com["members_hash"] in logged:
            continue
        similar = next((e for e in recent if jaccard(e["members"], com["members"]) >= args.similar), None)
        if similar:
            print(f"  skip 「{name['name']}」 (similar to 「{similar['community_name']}」 briefed {similar['created_at'][:10]})")
            continue
        terms = com["members"] + [a for a, c in aliases.items() if c in com["members"]]
        n_signals = len(signals_containing_terms(conn, terms, "all"))
        score = n_signals * (0.5 + _undertaught(com["members"], all_cov))
        candidates.append((score, com, name))

    candidates.sort(key=lambda x: -x[0])
    print(f"briefing candidates: {len(candidates)} (generating up to {args.max})")
    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")  # local date: filenames follow the writer's calendar
    written = 0
    for score, com, name in candidates[: args.max]:
        community_info = {
            "name": name["name"],
            "description": name["description"],
            "members": com["members"],
            "member_aliases": com["member_aliases"],
            "buffers": com["shared_buffers"],
            "top_verbs": com["top_verbs"],
            "strongest_links": com["strongest_links"],
        }
        terms = com["members"] + [a for a, c in aliases.items() if c in com["members"]]
        bundle = collect_bundle(conn, name["name"], terms, community_info, "all", args.max_signals)
        try:
            markdown = strip_fence(call_claude(render_prompt(bundle), model=args.model, timeout=900))
        except Exception as exc:  # noqa: BLE001 — one failed briefing shouldn't stop the rest
            print(f"  failed 「{name['name']}」: {exc}", file=sys.stderr)
            continue
        if not markdown.startswith(("---", "#")):
            print(f"  failed 「{name['name']}」: unexpected output head {markdown[:80]!r}", file=sys.stderr)
            continue
        out = BRIEFINGS_DIR / f"{today}-{_slugify(name['name'])}.md"
        if out.exists():
            out = out.with_name(f"{out.stem}-{com['members_hash'][:6]}.md")
        out.write_text(markdown + "\n", encoding="utf-8")
        save_briefing_log(
            conn, com["members_hash"], name["name"], com["members"],
            str(out.relative_to(REPO_ROOT)), model=args.model or "claude-cli",
        )
        written += 1
        print(f"  wrote {out.relative_to(REPO_ROOT)}  (score {score:.1f})")
    print(f"briefings written: {written}")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    conn = connect()
    topic_label, term_list, community_info = resolve_topic_terms(
        conn,
        community_name=args.community,
        term=args.term,
        terms_csv=args.terms,
        noun_mode=args.noun_mode,
    )
    bundle = collect_bundle(
        conn,
        topic_label,
        term_list,
        community_info,
        noun_mode=args.noun_mode,
        max_signals=args.max_signals,
    )
    if bundle["signal_total"] == 0:
        print(f"warning: no signals found for topic {topic_label!r}", file=sys.stderr)

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")  # local date: filenames follow the writer's calendar
    slug = _slugify(topic_label)
    bundle_path = TMP_DIR / f"{today}-{slug}.bundle.json"
    prompt_path = TMP_DIR / f"{today}-{slug}.prompt.md"
    output_path = BRIEFINGS_DIR / f"{today}-{slug}.md"

    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    prompt = render_prompt(bundle, output_path=output_path)
    prompt_path.write_text(prompt, encoding="utf-8")

    print(f"topic: {topic_label}")
    print(f"  terms:      {term_list[:8]}{'...' if len(term_list) > 8 else ''}")
    print(f"  signals:    {bundle['signal_total']} (top {len(bundle['signals'])} kept)")
    print(f"  bundle:     {bundle_path}")
    print(f"  prompt:     {prompt_path}")
    print(f"  target md:  {output_path}")
    print()
    print("Next: hand the prompt to a Claude subagent (or `claude -p < prompt.md`)")
    return 0


def cmd_suggest(args: argparse.Namespace) -> int:
    conn = connect()
    coms = list_community_names(conn)
    if not coms:
        print("no named communities in DB")
        return 1
    all_cov = read_all_note_coverage(conn)
    print(f"{'':4} {'name':40} {'signals':>7} {'undertaught':>11}")
    scored: list[tuple[float, dict]] = []
    for com in coms:
        members = com.get("members") or []
        sig_ids = signals_containing_terms(conn, members, args.noun_mode)
        # Undertaught score: fraction of member terms whose coverage < threshold
        low_cov = 0
        counted = 0
        for m in members[:10]:
            cov = all_cov.get(m)
            if cov is None:
                continue
            counted += 1
            if (cov.get("total") or 0) < 100:
                low_cov += 1
        undertaught = (low_cov / counted) if counted else 0.0
        score = len(sig_ids) * (0.5 + undertaught)
        scored.append((score, {"name": com["name"], "signals": len(sig_ids), "undertaught": undertaught}))
    scored.sort(key=lambda x: -x[0])
    for i, (score, info) in enumerate(scored[:args.top], start=1):
        print(f"{i:>4} {info['name']:40} {info['signals']:>7d} {info['undertaught']*100:>10.0f}%  score={score:.1f}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    src = Path(args.file)
    if not src.exists():
        print(f"error: file not found: {src}", file=sys.stderr)
        return 1
    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    dst = BRIEFINGS_DIR / src.name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {dst}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sug = sub.add_parser("suggest", help="Suggest topics ranked by signal count + undertaught note.com coverage")
    p_sug.add_argument("--top", type=int, default=10)
    p_sug.add_argument("--noun-mode", default="propn")
    p_sug.set_defaults(func=cmd_suggest)

    p_prep = sub.add_parser("prepare", help="Bundle a topic into a subagent prompt")
    p_prep.add_argument("--community", help="Named community from community_names table")
    p_prep.add_argument("--term", help="Single keyword")
    p_prep.add_argument("--terms", help="Comma-separated list of keywords")
    p_prep.add_argument("--noun-mode", default="all")
    p_prep.add_argument("--max-signals", type=int, default=25)
    p_prep.set_defaults(func=cmd_prepare)

    p_auto = sub.add_parser("auto", help="Generate briefings for new buffer-network communities via `claude -p`")
    p_auto.add_argument("--max", type=int, default=3, help="max briefings per run")
    p_auto.add_argument("--min-size", type=int, default=3)
    p_auto.add_argument("--similar", type=float, default=0.6, help="member Jaccard treated as the same topic")
    p_auto.add_argument("--cooldown-days", type=int, default=7)
    p_auto.add_argument("--max-signals", type=int, default=25)
    p_auto.add_argument("--model")
    p_auto.set_defaults(func=cmd_auto)

    p_imp = sub.add_parser("import", help="Copy a subagent-produced markdown into documents/briefings/")
    p_imp.add_argument("file")
    p_imp.set_defaults(func=cmd_import)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
