#!/usr/bin/env python3
"""秋田県のニュースとイベント情報をRSSで集め、要約と分類を付けて docs/articles.json に書き出す。

原文の本文はそのまま載せない。独自の短い要約・出典名・原文へのリンクだけを持たせる
（引用の範囲に収めるため）。

GitHub Actions から6時間ごとに実行され、差分がコミットされると
GitHub Pages 側のサイトが更新される。ローカルでも同じスクリプトが動く。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import llm_providers  # noqa: E402  （.env 読み込みより前でよい。キーは呼び出し時に参照される）

FEEDS_PATH = os.path.join(BASE_DIR, "feeds.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "articles.json")

USER_AGENT = "akita-news/1.0 (+https://github.com/mifune39428)"
FETCH_TIMEOUT = 25

# 新しく取り込む記事の対象期間。これより古い記事は拾わない。
INTAKE_DAYS = 3
# サイトに残す期間と件数の上限。イベントは会期まで間があるので長めに残す。
KEEP_DAYS = 21
KEEP_DAYS_EVENT = 45
KEEP_MAX = 400
# 1回のLLM呼び出しでまとめて処理する記事数。
# Groqの無料枠は分あたりトークン数（TPM 6000）が厳しいので、大きくし過ぎない。
BATCH_SIZE = 5
# 1回の実行で要約する上限。無料枠の1日あたり回数を使い切らないための蓋。
# 溢れた分は次の実行（6時間後）に回る。
MAX_NEW_PER_RUN = 40
# そのうちイベント検索から来た記事のために空けておく枠。
# 県内媒体のニュースを優先し切ると催し物がいつまでも載らないので、先に確保する。
EVENT_QUOTA = 10

CATEGORIES = [
    "行政・政治",
    "経済・企業",
    "観光・イベント",
    "グルメ・農林水産",
    "事件・事故",
    "天気・防災",
    "スポーツ",
    "文化・芸能",
    "教育・子育て",
    "くらし・地域",
    "その他",
]

AREAS = ["県北", "中央", "県南", "県全体"]

# Googleニュース経由で紛れ込むまとめサイト・アクセスランキング系の出典。
# 部分一致で落とす（増やすときはここに足す）。
BLOCK_SOURCES = ["じゃんごブログ"]

# Googleニュースの <source> がドメインのまま入ってくる媒体を、読める名前に直す。
DOMAIN_NAMES = {
    "akt.co.jp": "AKT秋田テレビ",
    "news.ntv.co.jp": "日テレNEWS",
    "news.yahoo.co.jp": "Yahoo!ニュース",
    "www3.nhk.or.jp": "NHK",
    "www.nhk.or.jp": "NHK",
    "chiba-tv.com": "チバテレ",
}

JST = dt.timezone(dt.timedelta(hours=9))

ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
RSS10 = "{http://purl.org/rss/1.0/}"


# --------------------------------------------------------------------------
# 下ごしらえ
# --------------------------------------------------------------------------

def load_env() -> None:
    """.env があれば読む（GitHub Actions では Secrets が環境変数で入るので不要）。"""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    """トラッキング用のクエリを落として、同じ記事が別URLに見えないようにする。"""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query)
        if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref", "at_"))
    ]
    # Google ニュース経由のリンクだけはクエリに記事IDが載るので触らない。
    if "news.google.com" in parts.netloc:
        query = urllib.parse.parse_qsl(parts.query)
    cleaned = parts._replace(query=urllib.parse.urlencode(query), fragment="")
    return urllib.parse.urlunsplit(cleaned).rstrip("/")


def article_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def parse_date(raw: str) -> dt.datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(raw)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# RSS / Atom / RDF の取得
# --------------------------------------------------------------------------

def _text(node, *paths: str) -> str:
    for path in paths:
        found = node.find(path)
        if found is not None:
            if found.text:
                return found.text
            # Atom の <link href="..."> のように属性側に入っている場合。
            href = found.get("href")
            if href:
                return href
    return ""


def fetch_feed(feed: dict) -> list[dict]:
    request = urllib.request.Request(feed["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        raw = response.read()

    root = ET.fromstring(raw)
    entries = (
        root.findall(".//item")
        or root.findall(f".//{RSS10}item")
        or root.findall(f".//{ATOM}entry")
    )

    items = []
    for entry in entries:
        title = strip_html(_text(entry, "title", f"{ATOM}title", f"{RSS10}title"))
        link = _text(entry, "link", f"{RSS10}link", f"{ATOM}link").strip()
        if not link:
            # Atom は複数の <link> を持つので rel="alternate" を拾う。
            for candidate in entry.findall(f"{ATOM}link"):
                if candidate.get("rel", "alternate") == "alternate":
                    link = (candidate.get("href") or "").strip()
                    break
        if not title or not link:
            continue

        source = feed["name"]
        if feed.get("google_news"):
            # Google ニュースの見出しは「本文の見出し - 媒体名」の形。
            # 媒体名は <source> にも入っているので、そちらを出典として使う。
            actual = strip_html(_text(entry, "source"))
            if actual:
                source = DOMAIN_NAMES.get(actual, actual)
                if title.endswith(f" - {actual}"):
                    title = title[: -len(actual) - 3].strip()
            else:
                title = re.sub(r"\s+-\s+[^-]{2,30}$", "", title).strip()
        if any(blocked in source for blocked in BLOCK_SOURCES):
            continue

        published = parse_date(
            _text(
                entry,
                "pubDate",
                f"{DC}date",
                f"{ATOM}published",
                f"{ATOM}updated",
                "date",
            )
        )
        body = strip_html(
            _text(
                entry,
                "description",
                f"{CONTENT}encoded",
                f"{RSS10}description",
                f"{ATOM}summary",
                f"{ATOM}content",
            )
        )
        # Google ニュースの description は他媒体へのリンク集なので要約の材料にならない。
        if feed.get("google_news"):
            body = ""

        items.append(
            {
                "id": article_id(link),
                "url": canonical_url(link),
                "title_original": title,
                "excerpt": body[:800],
                "source": source,
                "local": bool(feed.get("local")),
                "hint": feed.get("hint", ""),
                "published": (published or dt.datetime.now(dt.timezone.utc)).isoformat(),
            }
        )
    return items


def collect_feed_items(feeds: list[dict]) -> list[dict]:
    collected: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_feed, feed): feed for feed in feeds}
        for future in concurrent.futures.as_completed(futures):
            feed = futures[future]
            try:
                items = future.result()
            except Exception as exc:  # 1本落ちても全体は続ける
                print(f"  × {feed['name']}: {type(exc).__name__}: {exc}")
                continue
            print(f"  ○ {feed['name']}: {len(items)}件")
            collected.extend(items)
    return collected


# --------------------------------------------------------------------------
# 重複の除去
# --------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    title = re.sub(r"[\s　]+", "", title.lower())
    return re.sub(r"[!-/:-@\[-`{-~、。「」・…—–\-]", "", title)


TOKEN_RE = re.compile(r"[A-Za-z]{2,}|[0-9]+|[ァ-ヶー]{2,}|[一-龥]{2,}")


def title_tokens(title: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(title)}


def same_story(a: dict, b: dict) -> bool:
    """要約後の見出しで、同じ出来事を伝えているかどうかを見る。

    同じ発表を各社が報じると原題も文面も違うので、URLや原題だけでは重ならない。
    """
    published_a, published_b = parse_date(a["published"]), parse_date(b["published"])
    if published_a and published_b and abs((published_a - published_b).total_seconds()) > 36 * 3600:
        return False

    left, right = normalize_title(a["title_ja"]), normalize_title(b["title_ja"])
    if SequenceMatcher(None, left, right).ratio() >= 0.75:
        return True

    tokens_a, tokens_b = title_tokens(a["title_ja"]), title_tokens(b["title_ja"])
    if len(tokens_a) >= 3 and len(tokens_b) >= 3:
        if len(tokens_a & tokens_b) / len(tokens_a | tokens_b) >= 0.7:
            return True
    return False


def dedupe_stories(
    new_items: list[dict], existing_items: list[dict]
) -> tuple[list[dict], set[str]]:
    """同じ出来事を伝える記事は1本に絞る。県内媒体の記事を優先して残す。

    掲載する新着と、入れ替えで取り下げる既存記事のIDを返す。
    """
    recent = existing_items[:120]
    # 県内媒体（秋田魁・AAB・秋田経済新聞）を先に見て、後から来た重複を落とす。
    ordered = sorted(new_items, key=lambda item: 0 if item["local"] else 1)
    kept: list[dict] = []
    replaced: set[str] = set()
    for item in ordered:
        older = next(
            (o for o in recent if o["id"] not in replaced and same_story(item, o)), None
        )
        if older is not None:
            # 通信社の短報を先に載せたあとに県内媒体の記事が届いたら、そちらへ差し替える。
            if item["local"] and not older.get("local"):
                print(f"  ・県内媒体に差し替え: {item['title_ja']}（{item['source']}）")
                replaced.add(older["id"])
                kept.append(item)
            else:
                print(f"  ・既出のため除外: {item['title_ja']}（{item['source']}）")
            continue
        if any(same_story(item, other) for other in kept):
            print(f"  ・重複のため除外: {item['title_ja']}（{item['source']}）")
            continue
        kept.append(item)
    return kept, replaced


def is_duplicate(title: str, known_titles: list[str]) -> bool:
    target = normalize_title(title)
    if not target:
        return False
    for known in known_titles:
        if not known:
            continue
        if target == known:
            return True
        if abs(len(target) - len(known)) <= max(6, len(target) * 0.3):
            if SequenceMatcher(None, target, known).ratio() >= 0.86:
                return True
    return False


# --------------------------------------------------------------------------
# 要約と分類
# --------------------------------------------------------------------------

PROMPT_TEMPLATE = """あなたは秋田県のローカルニュースサイトの編集者です。
記事の見出しと抜粋を渡すので、県内の読者向けに「短い見出し」と「要約」を作り、分類してください。
今日は{today}です（日本時間）。

厳守すること:
- 原文をそのまま写さない。事実を踏まえて自分の言葉で短くまとめる。
- 事実を足さない。抜粋に書かれていない日付・場所・人数を創作しない。
  抜粋が無く見出しだけの場合は、見出しから確実に言えることだけを書く。
- 見出し(title_ja)は日本語で40文字以内。煽らず、内容が分かる形にする。
- 要約(summary_ja)は日本語で80〜140文字。1〜3文。
- akita: 秋田県（県内の市町村・団体・企業・人物・出来事）の話題なら true。
  秋田犬の他県での話題、「秋田」姓の人物、県外の出来事に秋田が少し出るだけの記事は false。
  広告・通販・番組宣伝・求人、まとめサイトのアクセスランキングなど報道でないものも false。
- kind: これから参加できる催し（祭り・フェス・展示・コンサート・体験会・相談会など）で、
  今日以降に開催されるものだけ "event"。既に終わった催しの報告記事は "news"。
- event_when: kind が event で開催時期が分かる場合だけ「8月13日〜15日」のような短い文字列。
  「本日」「今週末」のような相対表現は使わず、日付で書く。分からなければ空文字。
- area は次から1つ: {areas}
  県北=鹿角・大館・北秋田・能代・藤里・三種・八峰・小坂・上小阿仁 /
  中央=秋田市・男鹿・潟上・五城目・八郎潟・井川・大潟・由利本荘・にかほ /
  県南=大仙・仙北・美郷・横手・湯沢・羽後・東成瀬 /
  県全体=県全域の話題や地域が特定できないもの
- category は次から必ず1つ選ぶ: {categories}
- importance は1〜5の整数。5=県全体に関わる大きな発表や災害、3=普通のニュース、1=小ネタ。
- 出力はJSON配列のみ。前置き・説明・コードフェンスを付けない。

出力形式（要素数は入力と同じ{count}件、iは入力の番号）:
[{{"i":1,"akita":true,"kind":"news","title_ja":"...","summary_ja":"...","area":"中央","category":"行政・政治","importance":3,"event_when":""}}]

入力記事:
{articles}
"""


def build_prompt(batch: list[dict]) -> str:
    lines = []
    for index, item in enumerate(batch, start=1):
        lines.append(
            f"[{index}] 出典: {item['source']}\n"
            f"見出し: {item['title_original']}\n"
            f"抜粋: {item['excerpt'][:600] or '(抜粋なし)'}\n"
        )
    return PROMPT_TEMPLATE.format(
        today=dt.datetime.now(JST).strftime("%Y年%-m月%-d日"),
        areas=" / ".join(AREAS),
        categories=" / ".join(CATEGORIES),
        count=len(batch),
        articles="\n".join(lines),
    )


def parse_llm_json(text: str, expected: int) -> list[dict]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        raise llm_providers.ResponseInvalid("JSON配列が見つかりません")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise llm_providers.ResponseInvalid(f"JSONとして読めません: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise llm_providers.ResponseInvalid("空の配列です")
    if len(data) != expected:
        raise llm_providers.ResponseInvalid(f"{expected}件のはずが{len(data)}件です")
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("title_ja") or not entry.get("summary_ja"):
            raise llm_providers.ResponseInvalid("title_ja / summary_ja が欠けています")
    return data


def enrich(items: list[dict]) -> list[dict]:
    """LLMで見出し・要約・分類を付ける。失敗した分は捨てて次回に回す。"""
    results: list[dict] = []
    for offset in range(0, len(items), BATCH_SIZE):
        batch = items[offset : offset + BATCH_SIZE]
        print(f"  要約 {offset + 1}〜{offset + len(batch)}件目 …")
        try:
            text = llm_providers.generate_text(
                build_prompt(batch),
                validate=lambda t, n=len(batch): parse_llm_json(t, n),
            )
            entries = parse_llm_json(text, len(batch))
        except llm_providers.LLMError as exc:
            # 生煮えの記事をサイトに出すより、今回は見送って次の実行で拾い直す。
            # RSSには数日分残っているので、枠が空けば自然に再挑戦される。
            print(f"  × 要約に失敗（この{len(batch)}件は次回に回します）: {exc}")
            continue

        by_index = {}
        for entry in entries:
            try:
                by_index[int(entry.get("i", 0))] = entry
            except (TypeError, ValueError):
                continue

        for index, item in enumerate(batch, start=1):
            entry = by_index.get(index) or entries[index - 1]
            if entry.get("akita") is False:
                continue
            category = str(entry.get("category", "")).strip()
            area = str(entry.get("area", "")).strip()
            kind = str(entry.get("kind", "")).strip()
            item["title_ja"] = str(entry["title_ja"]).strip()
            item["summary_ja"] = str(entry["summary_ja"]).strip()
            item["category"] = category if category in CATEGORIES else "その他"
            item["area"] = area if area in AREAS else "県全体"
            # イベント検索から来た記事は、LLMが news と言わない限り催し物として扱う。
            is_event = kind == "event" or (item["hint"] == "event" and kind != "news")
            item["kind"] = "event" if is_event else "news"
            item["event_when"] = str(entry.get("event_when", "") or "").strip()[:40]
            try:
                item["importance"] = max(1, min(5, int(entry.get("importance", 3))))
            except (TypeError, ValueError):
                item["importance"] = 3
            results.append(item)
    return results


def to_public(item: dict) -> dict:
    """サイトに出す形に整える。原文の抜粋は公開データに残さない。"""
    return {
        key: value
        for key, value in item.items()
        if key not in ("excerpt", "hint")
    }


# --------------------------------------------------------------------------
# 保存
# --------------------------------------------------------------------------

def load_existing() -> dict:
    if not os.path.exists(OUTPUT_PATH):
        return {"updated_at": None, "items": []}
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"updated_at": None, "items": []}
    data.setdefault("items", [])
    return data


def main() -> int:
    load_env()

    with open(FEEDS_PATH, encoding="utf-8") as f:
        feeds = [feed for feed in json.load(f)["feeds"] if feed.get("enabled", True)]

    print(f"■ RSS取得（{len(feeds)}本）")
    fetched = collect_feed_items(feeds)
    print(f"  合計 {len(fetched)}件")

    existing = load_existing()
    existing_items = existing["items"]
    known_ids = {item["id"] for item in existing_items}
    known_urls = {canonical_url(item["url"]) for item in existing_items}
    known_titles = [normalize_title(item.get("title_original", "")) for item in existing_items]

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=INTAKE_DAYS)

    # 県内媒体を先に見ることで、同じ記事がGoogleニュース経由でも流れてきたときに
    # 元の媒体のURLのほうを残す。
    fetched.sort(key=lambda item: item["published"], reverse=True)
    fetched.sort(key=lambda item: 0 if item["local"] else 1)  # 安定ソートなので新しい順は保たれる

    new_items: list[dict] = []
    for item in fetched:
        published = parse_date(item["published"])
        if published is None or published < cutoff or published > now + dt.timedelta(hours=12):
            continue
        if item["id"] in known_ids or item["url"] in known_urls:
            continue
        if is_duplicate(item["title_original"], known_titles):
            continue
        known_ids.add(item["id"])
        known_urls.add(item["url"])
        known_titles.append(normalize_title(item["title_original"]))
        new_items.append(item)

    new_items.sort(key=lambda item: item["published"], reverse=True)

    print(f"■ 新着 {len(new_items)}件（重複と期間外を除外）")
    if len(new_items) > MAX_NEW_PER_RUN:
        print(f"  うち{MAX_NEW_PER_RUN}件を今回処理（残りは次回）")
        # イベントの枠を先に確保し、残りは県内媒体の記事から埋める。
        events = [item for item in new_items if item["hint"] == "event"][:EVENT_QUOTA]
        taken = {item["id"] for item in events}
        rest = [item for item in new_items if item["id"] not in taken]
        rest.sort(key=lambda item: 0 if item["local"] else 1)
        new_items = events + rest[: MAX_NEW_PER_RUN - len(events)]
        new_items.sort(key=lambda item: item["published"], reverse=True)

    enriched: list[dict] = []
    replaced: set[str] = set()
    if new_items:
        enriched = enrich(new_items)
        print(f"  要約 {len(enriched)}件（秋田と無関係と判定された分は除外）")
        enriched, replaced = dedupe_stories(enriched, existing_items)
        print(f"  掲載対象 {len(enriched)}件")

    # 既に載っている記事にも、あとから足した出典名の変換とブロックを効かせる。
    kept_existing = [
        {**item, "source": DOMAIN_NAMES.get(item["source"], item["source"])}
        for item in existing_items
        if item["id"] not in replaced
        and not any(blocked in item["source"] for blocked in BLOCK_SOURCES)
    ]

    merged = enriched + kept_existing
    merged = [
        item
        for item in merged
        if (parse_date(item.get("published", "")) or now)
        >= now - dt.timedelta(days=KEEP_DAYS_EVENT if item.get("kind") == "event" else KEEP_DAYS)
    ]
    merged.sort(key=lambda item: item["published"], reverse=True)
    merged = [to_public(item) for item in merged[:KEEP_MAX]]

    payload = {
        "updated_at": now.astimezone(JST).isoformat(),
        "categories": CATEGORIES,
        "areas": AREAS,
        "sources": sorted({item["source"] for item in merged}),
        "count": len(merged),
        "items": merged,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print(f"■ 書き出し: {OUTPUT_PATH}（掲載 {len(merged)}件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
