# 秋田ニュース＆イベント

秋田県のニュースと催し物のRSSを自動で集め、短い要約と分類を付けて静的サイトとして公開するツール。
GitHub Actions が6時間ごとに走り、GitHub Pages が更新される。

- サイト: https://mifune39428.github.io/akita-news/
- 更新: 日本時間の 3時 / 9時 / 15時 / 21時（GitHub Actions の cron）

## しくみ

```
RSS 9本 ──> collect.py ──> Gemini(→Groq→Claude→OpenAI) ──> docs/articles.json ──> GitHub Pages
         重複除去・期間絞り込み   要約＋エリア/カテゴリ分類・催し物判定       静的サイトが読む
```

- 原文の本文は保存も掲載もしない。**独自の要約・出典名・原文リンク**だけを持つ。
- LLMが秋田と無関係と判断した記事（秋田犬の県外の話題、「秋田」姓の人物、広告など）は自動で落とす。
- これから参加できる催しは `kind: event` として、サイト上で「イベント」に絞り込める。
- どのLLMも使えなかった分は保存せず、次の実行で拾い直す（生煮えの記事を出さないため）。

## ファイル

| ファイル | 役割 |
| --- | --- |
| `collect.py` | 収集・重複除去・要約・`docs/articles.json` の書き出し |
| `feeds.json` | 収集元のRSS一覧（`enabled: false` で一時停止できる） |
| `llm_providers.py` | LLMの多段フォールバック（dual_draft_poster からのコピー） |
| `docs/index.html` | サイト本体（依存なしの1ファイル、PWA対応） |
| `docs/articles.json` | 生成データ。Actions が自動コミットする |
| `.github/workflows/update.yml` | 6時間ごとの自動実行 |
| `更新.command` | Mac から手動で即更新（ダブルクリック） |

## 収集元

- 県内媒体の直接のRSS: 秋田魁新報 / AAB秋田朝日放送 / 秋田経済新聞
- Google ニュース検索のRSS: 「秋田県」「秋田＋イベント・祭り・フェス」「県内7市」、
  および ABS秋田放送・AKT秋田テレビ・NHK秋田（自前のRSSが無いのでサイト指定で拾う）

Google ニュース経由の記事は `<source>` から実際の媒体名を取り出し、
見出し末尾の「 - 媒体名」を落として掲載する。リンク先は Google ニュース の転送URL。

## 設定

- **GitHub Secrets**: `GEMINI_API_KEY` は必須。`GROQ_API_KEY` を入れておくとGeminiの無料枠が切れても止まらない。
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` は任意。
- **ローカル実行**: このフォルダの `.env` に同じキーを書く（gitignore 済み）。

## 調整しどころ（`collect.py` の定数）

- `INTAKE_DAYS = 3` — 何日前までの記事を取り込むか
- `MAX_NEW_PER_RUN = 40` — 1回の実行で要約する上限（無料枠を使い切らないための蓋）
- `EVENT_QUOTA = 10` — そのうちイベント検索の記事に確保する枠
- `BATCH_SIZE = 5` — 1回のLLM呼び出しでまとめる記事数（Groqの分間トークン制限に合わせてある）
- `KEEP_DAYS = 21` / `KEEP_DAYS_EVENT = 45` / `KEEP_MAX = 400` — サイトに残す期間と件数
- `BLOCK_SOURCES` — まとめサイトなど、出典名で丸ごと落としたい媒体
- `DOMAIN_NAMES` — Google ニュースの出典がドメインのまま入ってくる媒体の表示名

収集元を増やすときは `feeds.json` に足すだけでよい。
`hint` に `event` を入れたフィードは催し物として拾われやすくなる。

## 著作権について

各記事の権利は出典元にある。このサイトが持つのは自動生成した見出しと要約、
出典名、原文へのリンクだけで、本文の全訳や転載はしない。掲載停止の依頼があれば
`feeds.json` から該当媒体を外す。
