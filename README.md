# note-topix

複数のニュースソースを横断して「いま伸びていて、まだ書かれていない話題」を見つけ、記事の**問い**まで提案する個人用のリサーチパイプライン。note.com で技術記事を書くために作った。

```
7つのソース → 語の抽出 → 固有名詞ネットワーク → コミュニティ検出 → ブリーフィング生成
                                                                    ↓
                                          「なぜ〇〇なのか」という問い × 3
```

Claude Code で開発し、`claude -p` を使って分類・命名・ブリーフィング生成を自動化している。

## 何ができるか

- **7ソースの横断取り込み** — arXiv / Hacker News / Zenn / Qiita / Bluesky / Google Trends / Raindrop.io（X共有）
- **固有名詞の共起ネットワーク** — 後述の「バッファ重なり」方式。共起しない固有名詞同士を、文脈の重なりでつなぐ
- **話題の自動命名** — Louvain でコミュニティを検出し、共有している文脈語からテーマ名を付ける
- **ブリーフィング生成** — 話題ごとに、事実・論点・出典付きの数字・**「なぜ〇〇なのか」という問い3つ**をまとめた資料を生成
- **note.com の飽和度チェック** — 話題ごとに既存の有料記事数を調べ、「伸びているが手薄」な話題を優先
- **時系列の履歴** — 話題が続いているのか止まったのかを、実行ごとのスナップショットで追跡
- **Web UI** — ネットワークの可視化、ブリーフィング閲覧、原稿エディタ、画像管理、投稿記録

## 技術的な中心：バッファ重なりネットワーク

固有名詞（`claude`、`nvidia`、`qwen` など）を普通の共起で結ぶと、ほとんど辺が張れない。実データでは、**固有名詞を2つ以上含む記事は全体の8%しかなかった**。同じ記事に一緒に出てくることが稀だからだ。

そこで、固有名詞を **core**、その周辺の一般名詞と動詞を **buffer** として、

1. core の各言及について、**前後N個以内**の名詞・動詞を集める（文や行をまたがない）
2. 各 buffer 語を **PPMI** で重み付けする。期待値は「その語が**そのソース種別の中で**どれだけ出るか」から計算する（arXiv なら "benchmark" は当たり前なので割り引かれる）
3. 2つの core の buffer プロファイルの **cosine 類似度**を辺の重みにする

つまり「一緒に出てくるか」ではなく「**似た文脈で語られているか**」で結ぶ。結果として `openai – anthropic`（risk, regulations, frontier）、`llama – mistral – gemma – qwen`（オープンモデル群）のような、内容に基づいたまとまりが出る。

補助的に、次の処理を入れている。

- **表記ゆれの統合** — `hokusai` / `katsushika` → `katsushika hokusai` を LLM に判定させ、キャッシュする
- **buffer 語の選別** — 一般名詞のうち「話題の中身を表す語」だけを LLM に選ばせる（`framework` や `結果` は落ちる）
- **ソース定型文の除去** — Google Trends の配信元表記（`— Yahoo!ニュース`）や X のページタイトルの定型部分は、語の抽出前に落とす
- **辺の条件** — 共有する buffer 語が2つ以上ないと辺を張らない（1語だけの一致は偶然が多い）

## 構成

| ディレクトリ | 中身 |
| --- | --- |
| `scripts/fetch_*.py` | ソースごとの取り込み。出力は正規化した Markdown。冪等 |
| `scripts/analyze_signals.py` | 語の抽出（spaCy + Sudachi）、共起の重み（NPMI / PMI / Jaccard / Count） |
| `scripts/buffer_edges.py` | バッファ重なりネットワーク |
| `scripts/term_db.py` | SQLite のスキーマと読み書き |
| `scripts/run_pipeline.py` | 全体を回すワーカー（cron 用） |
| `scripts/generate_briefing.py` | ブリーフィング生成 |
| `scripts/name_communities.py` | コミュニティの命名 |
| `scripts/investigate.py` | 1つの問いについての事前調査（答えは書かない） |
| `scripts/publish_annex.py` | 記事の資料編を静的サイトとして書き出す |
| `scripts/articles.py` / `backlog.py` / `history.py` | 投稿記録、ネタ在庫、時系列 |
| `api/app.py` | FastAPI |
| `webapp/` | React + TypeScript + Vite の SPA |

## セットアップ

Python は [uv](https://docs.astral.sh/uv/) で管理している。

```bash
uv sync
uv run python -m spacy download en_core_web_sm   # pyproject で入らなかった場合
cp .env.example .env    # 値を埋める
```

LLM を使う処理は [Claude Code](https://claude.com/claude-code) の CLI を `claude -p` として呼ぶので、`claude` にログイン済みである必要がある。LLM を使わない範囲（取り込み・ネットワーク・可視化）はこれなしでも動く。

フロントエンドは pnpm。

```bash
cd webapp && pnpm install
```

## 使い方

```bash
# 全体を回す（取り込み → 語の抽出 → ネットワーク → note.com 飽和度 → LLM処理 → 履歴）
uv run scripts/run_pipeline.py

# 開発時
uv run scripts/run_pipeline.py --skip-fetch --skip-note-coverage --skip-llm

# ネットワークを CLI で覗く
uv run scripts/buffer_edges.py --window 5 --top 20
uv run scripts/buffer_edges.py --profile claude

# ネタ在庫（鮮度つき）と、問いの一覧
uv run scripts/backlog.py list --fresh --with-why -v
```

Web UI は2つのサーバを立てる。

```bash
uv run uvicorn api.app:app --reload --port 8080
cd webapp && pnpm dev       # http://127.0.0.1:5173
```

API は認証がないので、公開インターフェースにバインドしないこと。手元では Tailscale のアドレスに絞って使っている。

定期実行は cron で。

```cron
0 */6 * * * cd /path/to/note-topix && /path/to/uv run scripts/run_pipeline.py >> logs/pipeline.log 2>&1
```

## このリポジトリに入っていないもの

**コードだけを公開している。** 取り込んだ記事の本文、生成されたブリーフィング、記事の原稿、SQLite の DB は含まない。

- 取り込んだ記事本文は他者の著作物なので配布しない（手元では 300MB ほどになる）
- DB には LLM の分類結果が入っていて、それはソース本文に由来する
- ブリーフィングと原稿は個人の作業物

`uv run scripts/run_pipeline.py` を実行すれば、自分の環境で一から作られる。

## 注意点

- **日本語と英語の混在を前提にしている。** 英語は spaCy、日本語は Sudachi（SplitMode C）で処理する
- **不安定な API を使っている。** Zenn の記事一覧、note.com の検索、Google Trends の RSS はいずれも非公開の API で、予告なく変わりうる
- **スクレイピングの負荷に配慮している。** 取り込みは差分のみ、note.com の問い合わせには待ち時間を入れている。API の利用規約は各自で確認すること
- **LLM の呼び出しには課金が発生する。** 各処理は差分のみを対象にしているので、話題に変化がなければ呼び出しは起きない

## ライセンス

MIT
