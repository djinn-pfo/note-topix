#!/usr/bin/env python3
"""Annex site builder — the "資料編" behind each note article.

A note article should stand on its own and carry the author's own thinking.
The bulk material it rests on (the generated briefing, the research Q&A log,
quotable numbers with sources) goes here instead of bloating the article body,
and the article links to it.

Flow:

    uv run scripts/publish_annex.py new --briefing documents/briefings/<file>.md
      → scaffolds documents/annex/<date>-<slug>.md (frontmatter + briefing body
        + empty 調査ログ section). Edit it: paste the Claude Q&A, trim, add the
        note URL once the article is out.

    uv run scripts/publish_annex.py build [--out ../note-annex]
      → renders every annex entry to static HTML (index + one page each).

    uv run scripts/publish_annex.py publish [--out ../note-annex] [--message ...]
      → build, then commit and push that directory (a clone of the public
        GitHub Pages repo).

Entries with ``draft: true`` in their frontmatter are skipped by build, so an
annex page can be prepared before the article is published.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
ANNEX_DIR = REPO_ROOT / "documents" / "annex"
IMAGES_DIR = REPO_ROOT / "assets" / "images"
BRIEFINGS_DIR = REPO_ROOT / "documents" / "briefings"
DEFAULT_OUT = REPO_ROOT.parent / "note-annex"

sys.path.insert(0, str(SCRIPTS_DIR))
from analyze_signals import parse_frontmatter  # noqa: E402
from config import env  # noqa: E402

NOTE_USER = env("NOTE_USER")
SITE_TITLE = "note 記事の資料編"
SITE_DESCRIPTION = "note に書いた記事の裏側 — 自動生成したトピック、調べた事実、AI とのやり取りの記録。"

CSS = """
:root {
  --bg: #ffffff; --fg: #1f2937; --muted: #6b7280; --line: #e5e7eb;
  --accent: #2563eb; --card: #f8fafc; --code: #f1f5f9;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f172a; --fg: #e2e8f0; --muted: #94a3b8; --line: #1e293b;
    --accent: #60a5fa; --card: #1e293b; --code: #172033;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif;
  line-height: 1.85; font-size: 16px;
}
.wrap { max-width: 720px; margin: 0 auto; padding: 32px 16px 96px; }
header.site { border-bottom: 1px solid var(--line); margin-bottom: 32px; padding-bottom: 16px; }
header.site a { color: inherit; text-decoration: none; font-weight: 700; }
h1 { font-size: 1.6rem; line-height: 1.4; margin: 0 0 8px; }
h2 { font-size: 1.25rem; margin: 40px 0 12px; padding-top: 8px; border-top: 1px solid var(--line); }
h3 { font-size: 1.05rem; margin: 28px 0 8px; }
p, li { overflow-wrap: anywhere; }
a { color: var(--accent); }
.meta { color: var(--muted); font-size: 0.85rem; margin-bottom: 24px; }
.note-link {
  display: inline-block; background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; padding: 10px 14px; margin: 8px 0 24px; text-decoration: none;
}
.lead { color: var(--muted); font-size: 0.95rem; }
ul.entries { list-style: none; padding: 0; }
ul.entries li { border-bottom: 1px solid var(--line); padding: 14px 0; }
ul.entries a { font-weight: 600; text-decoration: none; font-size: 1.05rem; }
ul.entries .date { color: var(--muted); font-size: 0.8rem; display: block; }
code { background: var(--code); padding: 1px 5px; border-radius: 4px; font-size: 0.9em; }
pre { background: var(--code); padding: 12px; border-radius: 8px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote {
  margin: 16px 0; padding: 4px 16px; border-left: 3px solid var(--line); color: var(--muted);
}
table { border-collapse: collapse; width: 100%; font-size: 0.92rem; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; }
img { max-width: 100%; height: auto; border-radius: 8px; }
footer.site { margin-top: 64px; padding-top: 16px; border-top: 1px solid var(--line); color: var(--muted); font-size: 0.85rem; }
"""

PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<style>{css}</style>
</head>
<body>
<div class="wrap">
<header class="site"><a href="{root}index.html">{site_title}</a></header>
{body}
<footer class="site">{footer}</footer>
</div>
</body>
</html>
"""


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"[\s-]+", "-", text).strip("-").lower()[:60] or "annex"


def md_to_html(text: str) -> str:
    import markdown  # local import: only the build path needs it

    return markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])


# In the editor an image is referenced by the URL the app serves it from, so the
# preview renders. At build time those become site-relative paths and the files
# are copied next to the pages.
IMAGE_REF_RE = re.compile(r"\]\((?:/api/images/file/|images/)([^)\s]+)\)")


def rewrite_image_refs(md: str, used: set[str]) -> str:
    def repl(m: re.Match) -> str:
        name = unquote(m.group(1))
        used.add(name)
        return f"](../images/{quote(name)})"

    return IMAGE_REF_RE.sub(repl, md)


def copy_images(names: set[str], out_dir: Path) -> None:
    if not names:
        return
    target = out_dir / "images"
    target.mkdir(parents=True, exist_ok=True)
    for name in sorted(names):
        src = IMAGES_DIR / name
        if not src.exists():
            print(f"warning: image not found: {name}", file=sys.stderr)
            continue
        (target / name).write_bytes(src.read_bytes())
    print(f"  copied {len(names)} image(s)")


def read_entries(include_drafts: bool = False) -> list[dict]:
    entries = []
    for path in sorted(ANNEX_DIR.glob("*.md")):
        fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        if fm.get("draft") and not include_drafts:
            continue
        entries.append({
            "path": path,
            "slug": fm.get("slug") or path.stem,
            "title": str(fm.get("title") or path.stem),
            "date": str(fm.get("date") or ""),
            "note_url": str(fm.get("note_url") or ""),
            "summary": str(fm.get("summary") or ""),
            "body": body,
        })
    entries.sort(key=lambda e: (e["date"], e["slug"]), reverse=True)
    return entries


def _demote_headings(md: str) -> str:
    """Push briefing headings one level down so the annex page keeps a single H1."""
    return re.sub(r"^(#{1,5})(?= )", r"#\1", md, flags=re.MULTILINE)


def cmd_new(args: argparse.Namespace) -> int:
    briefing = Path(args.briefing)
    if not briefing.is_absolute():
        briefing = REPO_ROOT / briefing
    if not briefing.exists():
        print(f"error: briefing not found: {briefing}", file=sys.stderr)
        return 1
    fm, body = parse_frontmatter(briefing.read_text(encoding="utf-8"))
    title = args.title or str(fm.get("topic") or briefing.stem)
    today = datetime.now().strftime("%Y-%m-%d")
    slug = args.slug or slugify(title)
    out = ANNEX_DIR / f"{today}-{slug}.md"
    if out.exists() and not args.force:
        print(f"error: {out} exists (use --force)", file=sys.stderr)
        return 1
    ANNEX_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "---\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        f"date: {today}\n"
        f"slug: {slug}\n"
        # No inline comments: the frontmatter parser reads values verbatim.
        "note_url: \"\"\n"
        "summary: \"\"\n"
        "draft: true\n"
        f"source_briefing: {briefing.relative_to(REPO_ROOT)}\n"
        "---\n\n"
        "<!-- note_url に記事の URL、summary に一覧用の1行説明を入れ、\n"
        "     公開してよくなったら draft: false にする -->\n\n"
        "この記事の裏側の資料です。本文は note 側にあります。\n\n"
        "## 自動生成されたトピック\n\n"
        f"{_demote_headings(body.strip())}\n\n"
        "## 調査ログ\n\n"
        "（Claude に聞いた内容ややり取りをここに貼る）\n"
    )
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    print("次: 調査ログを追記し、note の URL と summary を埋めて draft: false にする")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser()
    entries = read_entries(include_drafts=args.drafts)
    if not entries:
        # Still render the index: the site can go live before the first entry.
        print("warning: no publishable entries yet (draft: true のみ)", file=sys.stderr)
    pages_dir = out_dir / "p"
    pages_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("")

    used_images: set[str] = set()
    for e in entries:
        note_link = (
            f'<a class="note-link" href="{html.escape(e["note_url"])}">📝 note の記事本文を読む</a>'
            if e["note_url"] else
            '<p class="lead">対応する note 記事はまだ公開されていません。</p>'
        )
        body = (
            f'<h1>{html.escape(e["title"])}</h1>'
            f'<div class="meta">{html.escape(e["date"])}</div>'
            f"{note_link}"
            f"{md_to_html(rewrite_image_refs(e['body'], used_images))}"
        )
        (pages_dir / f"{e['slug']}.html").write_text(
            PAGE.format(
                title=html.escape(f"{e['title']} | {SITE_TITLE}"),
                description=html.escape(e["summary"] or SITE_DESCRIPTION),
                css=CSS, root="../", site_title=html.escape(SITE_TITLE),
                body=body,
                footer=f'<a href="../index.html">資料編の一覧へ</a>',
            ),
            encoding="utf-8",
        )

    empty_note = (
        '<p class="lead">公開された資料編はまだありません。</p>' if not entries else ""
    )
    items = "\n".join(
        f'<li><a href="p/{e["slug"]}.html">{html.escape(e["title"])}</a>'
        f'<span class="date">{html.escape(e["date"])}'
        + (" · note 記事あり" if e["note_url"] else "")
        + "</span>"
        + (f'<p class="lead">{html.escape(e["summary"])}</p>' if e["summary"] else "")
        + "</li>"
        for e in entries
    )
    (out_dir / "index.html").write_text(
        PAGE.format(
            title=html.escape(SITE_TITLE),
            description=html.escape(SITE_DESCRIPTION),
            css=CSS, root="", site_title=html.escape(SITE_TITLE),
            body=f"<h1>{html.escape(SITE_TITLE)}</h1>"
                 f'<p class="lead">{html.escape(SITE_DESCRIPTION)}</p>'
                 f'{empty_note}<ul class="entries">{items}</ul>',
            footer=(
                f'note: <a href="https://note.com/{html.escape(NOTE_USER)}">{html.escape(NOTE_USER)}</a>'
                if NOTE_USER else ""
            ),
        ),
        encoding="utf-8",
    )
    copy_images(used_images, out_dir)
    print(f"built {len(entries)} pages into {out_dir}")
    for e in entries:
        print(f"  p/{e['slug']}.html  {e['title']}")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser()
    if not (out_dir / ".git").exists():
        print(f"error: {out_dir} is not a git clone of the annex repo", file=sys.stderr)
        return 1
    if cmd_build(args) != 0:
        return 1
    message = args.message or f"annex: update {datetime.now():%Y-%m-%d %H:%M}"
    subprocess.run(["git", "add", "-A"], cwd=out_dir, check=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=out_dir, capture_output=True, text=True)
    if not status.stdout.strip():
        print("no changes to publish")
        return 0
    subprocess.run(["git", "commit", "-m", message], cwd=out_dir, check=True)
    subprocess.run(["git", "push"], cwd=out_dir, check=True)
    print("pushed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="scaffold an annex entry from a briefing")
    p.add_argument("--briefing", required=True)
    p.add_argument("--title")
    p.add_argument("--slug")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("build", help="render annex entries to static HTML")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--drafts", action="store_true", help="include draft entries (local preview)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("publish", help="build, then commit and push the site repo")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--message")
    p.add_argument("--drafts", action="store_true")
    p.set_defaults(func=cmd_publish)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
