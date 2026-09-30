#!/usr/bin/env python3
"""Pre-research for one 「なぜ〇〇なのか」 question.

Answering a "why" question is what makes those articles good and also what
makes them expensive: you have to find the paper, the number, and the passage
that explains the mechanism. This collects that material in advance.

It deliberately does **not** answer the question. The article's value is the
author going "I didn't get X → I asked → oh, it's just a dot product", so the
memo stops at: what is known, where to look, what is still unclear.

    uv run scripts/investigate.py list --briefing documents/briefings/<f>.md
    uv run scripts/investigate.py run --briefing documents/briefings/<f>.md --question 2
    uv run scripts/investigate.py run --briefing <f>.md --question "なぜ〜なのか" --fetch

Writes documents/research/<date>-<slug>.md and marks the briefing as
`researched` in the backlog.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
RESEARCH_DIR = REPO_ROOT / "documents" / "research"

sys.path.insert(0, str(SCRIPTS_DIR))

from analyze_signals import parse_frontmatter  # noqa: E402
from backlog import WHY_RE  # noqa: E402
from llm import call_claude, strip_fence  # noqa: E402
from term_db import connect, set_backlog_state, signals_containing_terms  # noqa: E402

PROMPT = """あなたは、note で技術記事を書く研究者・エンジニアのリサーチ助手です。
書き手はこれから次の問いに答える記事を書きます。

# 問い

{question}

# 資料

## この問いが出てきたブリーフィングの該当部分

{briefing_excerpt}

## 関連するシグナル（論文要旨・記事本文）

{signals}

---

# あなたの仕事

**答えを書かないこと。** この記事の価値は、書き手自身が「分からない → 調べる →
腑に落ちる」過程を書くところにあります。あなたが結論を書いてしまうと、それが失われます。

やることは材料集めだけです。以下の Markdown を出力してください。

```markdown
# {question}

## 何が分かっているか (事実のみ)

**この問いに関係する事実だけ**を、数字は**何を測った数字か**まで添えて並べる。
同じトピックの資料でも、問いと関係が薄いものは載せない（無関係な発表やポイント数の
羅列は不要）。各項目に出典URLを付ける。推測は書かない。

- <事実>（<何を測ったか / 前提条件>）出典: <URL>

## どこを読めば仕組みが分かるか

答えに辿り着くために読むべき箇所を、具体的に指す。「この論文の何節」「この数式の
どの項」「この実装のどの関数」のレベルまで。**そこに何が書いてあるかの説明はしない**
(書き手が自分で読むため)。

- <読むべき箇所> — <なぜここを読む必要があるのか>

## まだ分かっていないこと

資料だけでは埋まらない穴。書き手が調べる、または AI に聞く必要がある点を具体的に。

- <不明点> — <確かめる方法>

## 用語の当たり (説明はしない)

この問いを追うのに前提として必要な用語を列挙するだけ。定義は書かない。

- <用語>

## 読む順番

1. <最初に読むもの> — <理由>
2. ...
```

# 制約

- 事実と出典以外は書かない。解釈・意見・結論は書き手の領分
- 資料に無いことを補完しない。無いなら「まだ分かっていないこと」に回す
- 出力は上記 Markdown 本文のみ。前置き・後書き・コードフェンスは不要
"""


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"[\s-]+", "-", text).strip("-")[:50] or "question"


def briefing_questions(path: Path) -> list[str]:
    _, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    return WHY_RE.findall(body)


def question_excerpt(path: Path, question: str) -> str:
    """The 「なぜ」 block for this question, plus the quotable-facts section."""
    _, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    out = []
    idx = body.find(question)
    if idx >= 0:
        rest = body[idx:]
        end = rest.find("\n- **なぜ", 1)
        out.append(rest[: end if end > 0 else 1200])
    facts = re.search(r"## 引用可能な事実・数字(.+?)(?=\n## )", body, re.DOTALL)
    if facts:
        out.append("### 引用可能な事実・数字\n" + facts.group(1).strip())
    return "\n\n".join(out) or body[:1500]


def topic_signals(conn, terms: list[str], limit: int, max_chars: int) -> str:
    sig_ids = signals_containing_terms(conn, terms, "all")
    if not sig_ids:
        return "(関連シグナルなし)"
    placeholders = ",".join("?" * len(sig_ids))
    rows = conn.execute(
        f"""
        SELECT source, title, url, body, engagement FROM signals
         WHERE signal_id IN ({placeholders}) ORDER BY engagement DESC LIMIT ?
        """,
        [*sig_ids, limit],
    ).fetchall()
    blocks = []
    for source, title, url, body, _ in rows:
        blocks.append(f"### [{source}] {title}\n{url}\n\n{(body or '')[:max_chars]}")
    return "\n\n".join(blocks)


def fetch_url_text(url: str, max_chars: int = 6000) -> str:
    """Plain-text-ish fetch for a cited page (arXiv abstract, blog post)."""
    try:
        html = subprocess.run(
            ["curl", "-sS", "-L", "--max-time", "25", "-A", "note-topix/0.1", url],
            capture_output=True, text=True, timeout=40,
        ).stdout
    except Exception:  # noqa: BLE001
        return ""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.DOTALL | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


def cmd_list(args: argparse.Namespace) -> int:
    path = Path(args.briefing)
    if not path.is_absolute():
        path = REPO_ROOT / path
    questions = briefing_questions(path)
    if not questions:
        print("このブリーフィングには「なぜ」の問いがありません（2026-09-24 以前の生成分）")
        return 1
    for i, q in enumerate(questions, start=1):
        print(f"{i}. {q}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    path = Path(args.briefing)
    if not path.is_absolute():
        path = REPO_ROOT / path
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    questions = briefing_questions(path)
    if args.question.isdigit():
        i = int(args.question)
        if not 1 <= i <= len(questions):
            raise SystemExit(f"問いは 1..{len(questions)} で指定してください")
        question = questions[i - 1]
    else:
        question = args.question

    conn = connect()
    terms = fm.get("terms") or []
    if isinstance(terms, str):
        terms = [t.strip() for t in terms.strip("[]").replace('"', "").split(",") if t.strip()]
    signals = topic_signals(conn, terms, args.max_signals, args.signal_chars)

    if args.fetch:
        urls = re.findall(r"https?://[^\s)\]]+", question_excerpt(path, question))[: args.fetch]
        extra = [f"### 取得: {u}\n{fetch_url_text(u)}" for u in urls]
        if extra:
            signals += "\n\n## 引用元から取得した本文\n\n" + "\n\n".join(extra)

    prompt = PROMPT.format(
        question=question,
        briefing_excerpt=question_excerpt(path, question),
        signals=signals,
    )
    print(f"問い: {question}\n資料: {len(prompt)} 文字を claude -p に渡します...")
    memo = strip_fence(call_claude(prompt, model=args.model, timeout=900))
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    out = RESEARCH_DIR / f"{datetime.now():%Y-%m-%d}-{slugify(question)}.md"
    out.write_text(
        f"---\nquestion: {question!r}\nbriefing: {path.relative_to(REPO_ROOT)}\n"
        f"date: {datetime.now():%Y-%m-%d}\n---\n\n{memo}\n",
        encoding="utf-8",
    )
    set_backlog_state(conn, str(path.relative_to(REPO_ROOT)), "researched", note_kind="why")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="show the なぜ questions of a briefing")
    p.add_argument("--briefing", required=True)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("run", help="collect material for one question")
    p.add_argument("--briefing", required=True)
    p.add_argument("--question", required=True, help="1-3 or the question text")
    p.add_argument("--max-signals", type=int, default=12)
    p.add_argument("--signal-chars", type=int, default=2500)
    p.add_argument("--fetch", type=int, default=0, metavar="N", help="also fetch N cited URLs")
    p.add_argument("--model")
    p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
