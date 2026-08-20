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
# 1回の実行で、過去の記事のサムネイルを取りに行く件数の上限。
BACKFILL_PER_RUN = 40

CATEGORIES = [
    "行政・政治",
    "経済・企業",
    "観光・イベント",
    "グルメ・農林水産",
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
MEDIA = "{http://search.yahoo.com/mrss/}"
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


# 記事のサムネイルとして使わない画像（配信計測用の透明画像やアイコンなど）。
IMAGE_BLOCKLIST = ("feedburner", "gravatar", "/pixel", "1x1", "blank.gif", "spacer", "doubleclick")
IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\']'
    r'[^>]+content=["\']([^"\']+)["\']|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
    r'(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\']',
    re.I,
)


def usable_image(url: str, base: str) -> str:
    url = html.unescape((url or "").strip())
    if not url:
        return ""
    url = urllib.parse.urljoin(base, url)
    if not url.startswith(("http://", "https://")):
        return ""
    if any(word in url.lower() for word in IMAGE_BLOCKLIST):
        return ""
    return url


def image_from_entry(entry, base: str) -> str:
    """RSSの中に入っている画像を探す。媒体ごとに置き場所が違うので順に当たる。"""
    for node in entry.findall(f"{MEDIA}thumbnail") + entry.findall(f"{MEDIA}content"):
        medium = (node.get("medium") or node.get("type") or "").lower()
        if medium and "image" not in medium:
            continue
        found = usable_image(node.get("url", ""), base)
        if found:
            return found

    for node in entry.findall("enclosure") + entry.findall(f"{ATOM}link"):
        if "image" in (node.get("type") or "").lower():
            found = usable_image(node.get("url") or node.get("href") or "", base)
            if found:
                return found

    # 本文HTMLの最初の <img>。多くの媒体はここにアイキャッチが入っている。
    raw_body = " ".join(
        node.text or ""
        for tag in ("description", f"{CONTENT}encoded", f"{RSS10}description",
                    f"{ATOM}summary", f"{ATOM}content")
        for node in entry.findall(tag)
    )
    for candidate in IMG_TAG_RE.findall(raw_body):
        found = usable_image(candidate, base)
        if found:
            return found
    return ""


# --------------------------------------------------------------------------
# Google ニュースのリンクを元媒体のURLに戻す
# --------------------------------------------------------------------------

GOOGLE_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
SIGNATURE_RE = re.compile(r'data-n-a-sg="([^"]+)"')
TIMESTAMP_RE = re.compile(r'data-n-a-ts="([^"]+)"')


def resolve_google_url(url: str) -> str:
    """news.google.com の転送URLから、元媒体の記事URLを取り出す。

    転送ページはJavaScriptで飛ぶ作りなので、HTTPを追うだけでは元URLが分からない。
    ページに埋まっている署名（sg）と時刻（ts）を Google の batchexecute に投げると
    元URLが返る。取れなければ転送URLのまま使う（リンクとしては機能する）。
    """
    if "news.google.com" not in url:
        return url
    try:
        article_id = url.split("/articles/")[1].split("?")[0]
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            # 署名はページのかなり後ろに入っているので、途中で切らずに全部読む。
            page = response.read().decode("utf-8", errors="ignore")
        signature, timestamp = SIGNATURE_RE.search(page), TIMESTAMP_RE.search(page)
        if not signature or not timestamp:
            return url

        payload = [[
            "Fbv4je",
            json.dumps([
                "garturlreq",
                [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                  None, None, None, None, None, 0, 1],
                 "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                article_id, int(timestamp.group(1)), signature.group(1),
            ]),
            None, "1",
        ]]
        data = urllib.parse.urlencode({"f.req": json.dumps([payload])}).encode()
        request = urllib.request.Request(
            GOOGLE_BATCH_URL,
            data=data,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001  取れなくても転送URLで記事は読める
        return url
    return parse_garturlres(body) or url


def parse_garturlres(body: str) -> str:
    """batchexecute の返事から元URLを取り出す。

    返事は `[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://…\\",1]",…]]` の形で、
    URLは二重にJSONエスケープされている。素直に2段階で読む。
    """
    for line in body.splitlines():
        if "garturlres" not in line:
            continue
        try:
            for part in json.loads(line):
                if isinstance(part, list) and len(part) > 2 and part[0] == "wrb.fr":
                    inner = json.loads(part[2])
                    if len(inner) > 1 and str(inner[1]).startswith("http"):
                        return canonical_url(inner[1])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return ""


def resolve_google_urls(items: list[dict]) -> None:
    targets = [item for item in items if "news.google.com" in item["url"]]
    if not targets:
        return
    print(f"  Googleニュースのリンク {len(targets)}件を元媒体のURLに変換中 …")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item, resolved in zip(targets, pool.map(lambda i: resolve_google_url(i["url"]), targets)):
            item["url"] = resolved
    remaining = sum(1 for item in targets if "news.google.com" in item["url"])
    print(f"  変換できたもの {len(targets) - remaining}件")


def fetch_og_image(url: str) -> str:
    """RSSに画像が無い記事は、元ページの og:image を見に行く。"""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=12) as response:
            head = response.read(200_000).decode("utf-8", errors="ignore")
            final_url = response.geturl()
    except Exception:  # noqa: BLE001  取れなくても記事自体は載せる
        return ""
    match = OG_IMAGE_RE.search(head)
    if not match:
        return ""
    return usable_image(match.group(1) or match.group(2) or "", final_url)


def fill_missing_images(items: list[dict], limit: int = 0) -> None:
    """画像がまだ無い記事について、元ページの og:image を取りに行く。

    limit を渡すと1回に取りに行く件数を抑える（既存記事の穴埋め用）。
    """
    targets = [item for item in items if not item.get("image")]
    if limit:
        targets = targets[:limit]
    if not targets:
        return
    print(f"  サムネイル未取得 {len(targets)}件をページから取得中 …")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item, image in zip(targets, pool.map(lambda i: fetch_og_image(i["url"]), targets)):
            item["image"] = image
    print(f"  取得できたもの {sum(1 for item in targets if item['image'])}件")


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
                "image": image_from_entry(entry, link),
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
- negative: 読んで気が滅入る話題なら true。このサイトには載せない。
  事件・事故・犯罪・逮捕・裁判・訴訟・不祥事・自殺・訃報・お悔やみ・人身被害・
  火災・遭難・クマなどによる人的被害・倒産・詐欺被害 は true。
  ただし「大雨注意報」「クマの目撃情報」のような、これから身を守るための注意喚起は false
  （被害そのものの報道ではなく、事前に知って役立つ情報なので載せる）。
  スポーツの敗戦や制度の課題を扱う記事も、事件・事故でなければ false でよい。
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
[{{"i":1,"akita":true,"negative":false,"kind":"news","title_ja":"...","summary_ja":"...","area":"中央","category":"行政・政治","importance":3,"event_when":""}}]

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
            if entry.get("negative") is True:
                print(f"  ・暗い話題のため除外: {entry.get('title_ja', '')}")
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
# 秋田市の天気
# --------------------------------------------------------------------------

# 天気と降水確率は気象庁の秋田県予報を使う（予報官が出した公式の値）。
# data[0] が今日・明日（6時間ごとの降水確率つき）、data[1] が週間予報。
JMA_FORECAST_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/050000.json"
JMA_COAST_AREA = "050010"   # 秋田市が入る「沿岸」区域
JMA_PREF_AREA = "050000"    # 週間予報の区域（秋田県）
# 気温と、気象庁の週間予報が届かない先の日だけ Open-Meteo で埋める（APIキー不要）。
# 座標は秋田市役所あたり。
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=39.7186&longitude=140.1024"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone=Asia%2FTokyo&forecast_days=10"
)
# いつも見ている天気サイトへの入り口（秋田市のページ）。
# tenki.jp は規約でデータの取得・転載を禁じているので、リンクを置くだけにする。
TENKI_URL = "https://tenki.jp/forecast/2/8/3210/5201/"

FORECAST_DAYS = 10

# 気象庁の天気コード（テロップ番号）→ 絵文字と短い言い方。
# 表にない番号は先頭の数字（1=晴れ / 2=くもり / 3=雨 / 4=雪）で拾う。
JMA_TELOP = {
    "100": ("☀️", "晴れ"), "101": ("🌤", "晴れ時々くもり"), "102": ("🌦", "晴れ一時雨"),
    "103": ("🌦", "晴れ時々雨"), "104": ("🌨", "晴れ一時雪"), "105": ("🌨", "晴れ時々雪"),
    "106": ("🌦", "晴れ一時雨か雪"), "107": ("🌦", "晴れ時々雨か雪"),
    "110": ("🌤", "晴れのち時々くもり"), "111": ("🌤", "晴れのちくもり"),
    "112": ("🌦", "晴れのち一時雨"), "113": ("🌦", "晴れのち時々雨"), "114": ("🌧", "晴れのち雨"),
    "115": ("🌨", "晴れのち一時雪"), "116": ("🌨", "晴れのち時々雪"), "117": ("🌨", "晴れのち雪"),
    "119": ("⛈", "晴れのち雨か雷雨"), "125": ("⛈", "晴れ午後は雷雨"),
    "126": ("🌦", "晴れ昼頃から雨"), "127": ("🌦", "晴れ夕方から雨"), "128": ("🌦", "晴れ夜は雨"),
    "200": ("☁️", "くもり"), "201": ("⛅", "くもり時々晴れ"), "202": ("🌦", "くもり一時雨"),
    "203": ("🌦", "くもり時々雨"), "204": ("🌨", "くもり一時雪"), "205": ("🌨", "くもり時々雪"),
    "206": ("🌦", "くもり一時雨か雪"), "207": ("🌦", "くもり時々雨か雪"), "209": ("🌫", "霧"),
    "210": ("⛅", "くもりのち時々晴れ"), "211": ("⛅", "くもりのち晴れ"),
    "212": ("🌦", "くもりのち一時雨"), "213": ("🌦", "くもりのち時々雨"), "214": ("🌧", "くもりのち雨"),
    "215": ("🌨", "くもりのち一時雪"), "216": ("🌨", "くもりのち時々雪"), "217": ("🌨", "くもりのち雪"),
    "218": ("🌨", "くもりのち雨か雪"), "219": ("⛈", "くもりのち雨か雷雨"),
    "224": ("🌦", "くもり昼頃から雨"), "225": ("🌦", "くもり夕方から雨"), "226": ("🌦", "くもり夜は雨"),
    "228": ("🌨", "くもり昼頃から雪"), "229": ("🌨", "くもり夕方から雪"), "230": ("🌨", "くもり夜は雪"),
    "300": ("🌧", "雨"), "301": ("🌦", "雨時々晴れ"), "302": ("🌦", "雨時々やむ"),
    "303": ("🌨", "雨時々雪"), "306": ("🌧", "大雨"), "308": ("🌧", "雨で暴風を伴う"),
    "311": ("🌦", "雨のち晴れ"), "313": ("🌧", "雨のちくもり"), "314": ("🌨", "雨のち時々雪"),
    "315": ("🌨", "雨のち雪"), "317": ("🌦", "雨か雪のち晴れ"), "320": ("🌦", "朝のうち雨のち晴れ"),
    "321": ("🌧", "朝のうち雨のちくもり"), "328": ("🌧", "雨で夜は暴風雨"),
    "400": ("❄️", "雪"), "401": ("🌨", "雪時々晴れ"), "402": ("🌨", "雪時々やむ"),
    "403": ("🌨", "雪時々雨"), "406": ("❄️", "風雪強い"), "407": ("❄️", "大雪"),
    "411": ("🌤", "雪のち晴れ"), "413": ("☁️", "雪のちくもり"), "414": ("🌧", "雪のち雨"),
    "420": ("🌤", "朝のうち雪のち晴れ"), "421": ("☁️", "朝のうち雪のちくもり"),
    "425": ("❄️", "雪一時強く降る"),
}
JMA_TELOP_FALLBACK = {"1": ("☀️", "晴れ"), "2": ("☁️", "くもり"), "3": ("🌧", "雨"), "4": ("❄️", "雪")}

# Open-Meteo の天気コード → 絵文字と言い方（気象庁の週間予報より先の日に使う）。
OM_CODES = {
    0: ("☀️", "快晴"), 1: ("🌤", "晴れ"), 2: ("⛅", "晴れ時々くもり"), 3: ("☁️", "くもり"),
    45: ("🌫", "霧"), 48: ("🌫", "霧"), 51: ("🌦", "霧雨"), 53: ("🌦", "霧雨"), 55: ("🌦", "強い霧雨"),
    56: ("🌧", "着氷性の霧雨"), 57: ("🌧", "着氷性の霧雨"),
    61: ("🌧", "小雨"), 63: ("🌧", "雨"), 65: ("🌧", "強い雨"),
    66: ("🌧", "着氷性の雨"), 67: ("🌧", "着氷性の雨"),
    71: ("🌨", "小雪"), 73: ("🌨", "雪"), 75: ("❄️", "大雪"), 77: ("❄️", "細氷"),
    80: ("🌦", "にわか雨"), 81: ("🌦", "にわか雨"), 82: ("⛈", "激しいにわか雨"),
    85: ("🌨", "にわか雪"), 86: ("🌨", "にわか雪"),
    95: ("⛈", "雷雨"), 96: ("⛈", "雷雨・ひょう"), 99: ("⛈", "雷雨・ひょう"),
}


def fetch_json(url: str, timeout: int = 15):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def telop(code: str) -> tuple[str, str]:
    code = str(code or "").strip()
    return JMA_TELOP.get(code) or JMA_TELOP_FALLBACK.get(code[:1], ("", "―"))


def to_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def find_area(series: dict, code: str) -> dict:
    return next(a for a in series["areas"] if a["area"]["code"] == code)


def jma_forecast() -> tuple[dict, list[dict], str]:
    """気象庁から、日ごとの天気・降水確率と、6時間ごとの降水確率を取り出す。

    返すのは {日付: {icon, text, pop}} と、6時間ごとの降水確率のコマ、発表時刻。
    """
    data = fetch_json(JMA_FORECAST_URL)
    days: dict[str, dict] = {}

    # 今日・明日：天気の文と6時間ごとの降水確率。
    near = data[0]["timeSeries"]
    weather = find_area(near[0], JMA_COAST_AREA)
    for stamp, code, text in zip(
        near[0]["timeDefines"], weather["weatherCodes"], weather["weathers"]
    ):
        icon, _ = telop(code)
        days[stamp[:10]] = {
            "icon": icon,
            # 「晴れ　時々　くもり」のように全角スペースで区切られているので詰める。
            "text": re.sub(r"[　\s]+", "", text),
            "pop": None,
            "official": True,
        }

    blocks: list[dict] = []
    pops_series = find_area(near[1], JMA_COAST_AREA)
    for stamp, value in zip(near[1]["timeDefines"], pops_series["pops"]):
        pop = to_int(value)
        if pop is None:
            continue
        start = int(stamp[11:13])
        blocks.append({"time": stamp[:16], "label": f"{start}-{start + 6}時", "pop": pop})
        day = days.setdefault(stamp[:10], {"icon": "", "text": "", "pop": None, "official": True})
        # その日の代表値は、6時間ごとのうち一番高い確率にする。
        day["pop"] = pop if day["pop"] is None else max(day["pop"], pop)

    # 週間予報：3日目以降の天気と降水確率。
    week = data[1]["timeSeries"][0]
    weekly = find_area(week, JMA_PREF_AREA)
    for stamp, code, value in zip(week["timeDefines"], weekly["weatherCodes"], weekly["pops"]):
        date = stamp[:10]
        if date in days:  # 今日・明日は細かいほうの予報を優先する
            continue
        icon, text = telop(code)
        days[date] = {"icon": icon, "text": text, "pop": to_int(value), "official": True}

    return days, blocks, data[0]["reportDatetime"]


def fetch_weather() -> dict:
    """秋田市の天気（今日・明日、6時間ごとの降水確率、10日間）をまとめる。

    天気と降水確率は気象庁、気温は Open-Meteo。気象庁の週間予報より先の日だけ、
    天気と降水確率も Open-Meteo の予測で埋める（その日は official を false にする）。
    """
    try:
        forecast = fetch_json(OPEN_METEO_URL)
    except Exception as exc:  # noqa: BLE001
        print(f"  × 気温の取得に失敗: {type(exc).__name__}: {exc}")
        return {}
    try:
        jma_days, blocks, reported_at = jma_forecast()
    except Exception as exc:  # noqa: BLE001  気象庁が読めない日は数値予報だけで出す
        print(f"  × 気象庁の予報が読めません（Open-Meteoで代用）: {type(exc).__name__}: {exc}")
        jma_days, blocks, reported_at = {}, [], ""

    daily = forecast["daily"]
    days = []
    for i, date in enumerate(daily["time"][:FORECAST_DAYS]):
        entry = jma_days.get(date)
        if entry is None:
            icon, text = OM_CODES.get(daily["weather_code"][i], ("", "―"))
            entry = {
                "icon": icon,
                "text": text,
                "pop": daily["precipitation_probability_max"][i],
                "official": False,
            }
        days.append({
            "date": date,
            "max": daily["temperature_2m_max"][i],
            "min": daily["temperature_2m_min"][i],
            **entry,
        })

    official = sum(1 for day in days if day["official"])
    print(f"  天気: {len(days)}日分（うち気象庁 {official}日）/ 6時間ごと {len(blocks)}コマ")
    return {
        "city": "秋田市",
        "reported_at": reported_at,
        "updated_at": dt.datetime.now(JST).isoformat(),
        "days": days,
        "pops": blocks,
        "link": TENKI_URL,
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
        print(f"  要約 {len(enriched)}件（秋田と無関係・暗い話題と判定された分は除外）")
        enriched, replaced = dedupe_stories(enriched, existing_items)
        print(f"  掲載対象 {len(enriched)}件")
        # 実際に載せる記事だけ元ページを見に行く（無駄なアクセスを増やさないため）。
        resolve_google_urls(enriched)
        fill_missing_images(enriched)

    # 既に載っている記事にも、あとから足した出典名の変換とブロック、
    # それに「事件・事故は載せない」という方針を後追いで効かせる。
    kept_existing = [
        {**item, "source": DOMAIN_NAMES.get(item["source"], item["source"])}
        for item in existing_items
        if item["id"] not in replaced
        and item.get("category") in CATEGORIES
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
    merged = merged[:KEEP_MAX]

    # 以前の実行で画像が付かなかった記事を、少しずつ埋めていく。
    stale = [item for item in merged if not item.get("image")][:BACKFILL_PER_RUN]
    if stale:
        print("■ 既存記事のサムネイル補完")
        resolve_google_urls(stale)
        fill_missing_images(stale)

    print("■ 秋田市の天気")
    weather = fetch_weather() or existing.get("weather") or {}

    merged = [to_public(item) for item in merged]

    payload = {
        "updated_at": now.astimezone(JST).isoformat(),
        "categories": CATEGORIES,
        "areas": AREAS,
        "weather": weather,
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
