#!/usr/bin/env python3
"""一覧の先頭に出す「ここ数日のまとめ」を作る。

ユニクロ ニュースとミニマリスト・ニュースに先にあった仕組みを、
秋田・Apple・グリーン・デイ・やさしいニュースの4サイトで共通に使えるよう切り出したもの。
この digest.py は4つのフォルダに同じものを置いている（llm_providers.py と同じ持ち方）。
直すときは4つとも揃えること。サイトごとの違いは collect.py の DIGEST_SITE に書く。

作り方の約束:
  - 材料は、すでにサイトに載せている見出しと要約だけ。原文は読みに行かない。
  - 渡した記事に無い数字・日付・固有名詞を足させない。
  - 材料の顔ぶれが前回と同じなら、LLMを呼ばずに前回のまとめを使う。
  - 作れなかった回は前回のまとめを残す（古くても、空欄よりは役に立つ）。
    まとめ作りでつまずいても、ニュースの更新そのものは止めない。

単体で動かすと、記事を集め直さずにまとめだけ作り直せる:
    python3 digest.py            # docs/articles.json のまとめを作り直して書き戻す
    python3 digest.py --dry-run  # 作って画面に出すだけ（書き戻さない）
    python3 digest.py --force    # 材料が前回と同じでも作り直す
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys

import llm_providers

JST = dt.timezone(dt.timedelta(hours=9))

DIGEST_DAYS = 3
DIGEST_MAX_ITEMS = 24
# これより少ない日は、まとめるほどの材料が無いので前回のものを残す。
DIGEST_MIN_ITEMS = 3

PROMPT = """あなたは{editor}です。
ここ{days}日に載せた記事を渡すので、サイトの一番上に出す「ここ{days}日のまとめ」を書いてください。
今日は{today}です（日本時間）。

厳守すること:
- 渡した記事に書かれていることだけを使う。数字・日付・価格・人名を足さない。
- headline は全体を一言で表す30文字以内の見出し。
- points は3〜5個。1つ70文字以内の日本語の文。似た話題はまとめて1つにする。
  重要度の高い記事を優先する。
- {focus}
- 数字は算用数字で書く（9月、3,500円、5日間）。漢数字は使わない。
- 各 point の ids には、その文の根拠にした記事の id を1〜3個入れる。
- 出力はJSONオブジェクトのみ。前置き・説明・コードフェンスを付けない。

出力形式:
{{"headline": "...", "points": [{{"text": "...", "ids": ["..."]}}]}}

記事（{columns}）:
{articles}
"""


def parse_digest_json(text: str, known_ids: set[str]) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise llm_providers.ResponseInvalid("JSONオブジェクトが見つかりません")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise llm_providers.ResponseInvalid(f"JSONとして読めません: {exc}") from exc
    headline = str(data.get("headline", "")).strip()
    points = data.get("points")
    if not headline or not isinstance(points, list) or not 2 <= len(points) <= 6:
        raise llm_providers.ResponseInvalid("headline / points（3〜5個）が揃っていません")
    cleaned_points = []
    for point in points:
        if not isinstance(point, dict) or not str(point.get("text", "")).strip():
            raise llm_providers.ResponseInvalid("point に text がありません")
        text_value = str(point["text"]).strip()
        if "http://" in text_value or "https://" in text_value:
            raise llm_providers.ResponseInvalid("point にURLが入っています")
        ids = [str(i) for i in (point.get("ids") or []) if str(i) in known_ids][:3]
        cleaned_points.append({"text": text_value, "ids": ids})
    # 根拠の記事が1つも付いていないまとめは、渡した記事を見ずに書いたおそれがある。
    if not any(p["ids"] for p in cleaned_points):
        raise llm_providers.ResponseInvalid("どの point にも根拠の記事（ids）がありません")
    return {"headline": headline[:40], "points": cleaned_points}


def _published(item: dict, parse_date, fallback: dt.datetime) -> dt.datetime:
    return parse_date(item.get("published", "")) or fallback


def build_digest(
    items: list[dict],
    previous: dict | None,
    site: dict,
    parse_date,
    force: bool = False,
) -> dict | None:
    """ここ数日の記事から、一覧の先頭に出す短いまとめを作る。

    site には collect.py の DIGEST_SITE を渡す:
        editor   … 「あなたは◯◯です」に入る、編集者の立場
        focus    … そのサイトならではの注意（1文）
        columns  … 記事1行の項目名（プロンプトに見出しとして出す）
        describe … 記事1件を「項目 / 項目 / …」の1行にする関数
    """
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=DIGEST_DAYS)
    recent = [i for i in items if _published(i, parse_date, now) >= cutoff]
    recent.sort(key=lambda i: (i.get("importance", 3), i.get("published", "")), reverse=True)
    recent = recent[:DIGEST_MAX_ITEMS]
    if len(recent) < DIGEST_MIN_ITEMS:
        print(f"■ ここ{DIGEST_DAYS}日の記事が{len(recent)}件しかないため、前回のまとめを残します")
        return previous

    signature = hashlib.sha1(json.dumps(sorted(i["id"] for i in recent)).encode()).hexdigest()[:12]
    if not force and previous and previous.get("sig") == signature:
        return previous

    lines = []
    for item in recent:
        when = _published(item, parse_date, now).astimezone(JST)
        lines.append(f"{item['id']} / {when.month}月{when.day}日 / {site['describe'](item)}")
    prompt = PROMPT.format(
        editor=site["editor"],
        focus=site["focus"],
        columns="id / 公開日 / " + site["columns"],
        days=DIGEST_DAYS,
        today=f"{dt.datetime.now(JST):%Y年}{dt.datetime.now(JST).month}月{dt.datetime.now(JST).day}日",
        articles="\n".join(lines),
    )
    known = {i["id"] for i in recent}
    print(f"■ ここ{DIGEST_DAYS}日のまとめを作成（記事{len(recent)}件）")
    try:
        raw = llm_providers.generate_text(prompt, validate=lambda t: parse_digest_json(t, known))
        digest = parse_digest_json(raw, known)
    except Exception as exc:  # noqa: BLE001  まとめで転んでもニュースの更新は止めない
        print(f"  × まとめの作成に失敗（前回のものを残します）: {exc}")
        return previous
    digest.update({
        "days": DIGEST_DAYS,
        "from": cutoff.astimezone(JST).isoformat(),
        "generated_at": dt.datetime.now(JST).isoformat(),
        "article_count": len(recent),
        "sig": signature,
    })
    print(f"  ○ {digest['headline']}（{len(digest['points'])}項目）")
    return digest


def main() -> int:
    """記事を集め直さずに、いまの articles.json のまとめだけ作り直す。"""
    import collect  # noqa: PLC0415  循環を避けるため、単体で動かすときだけ読む

    dry_run = "--dry-run" in sys.argv
    collect.load_env()
    path = collect.OUTPUT_PATH
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    digest = build_digest(
        payload.get("items", []), payload.get("digest"), collect.DIGEST_SITE,
        collect.parse_date, force="--force" in sys.argv,
    )
    if not digest:
        print("まとめを作れませんでした。")
        return 1
    by_id = {i["id"]: i for i in payload.get("items", [])}
    print(f"\n【{digest['headline']}】")
    for n, point in enumerate(digest["points"], 1):
        print(f" {n}. {point['text']}")
        for ref in point["ids"]:
            print(f"      ← {by_id[ref]['source']}｜{by_id[ref]['title_ja'][:44]}")
    if dry_run:
        print("\n（確認のみ。書き戻していません）")
        return 0
    # 「items」の手前に置く（ユニクロ・ミニマリストと同じ並び）。
    rebuilt = {k: v for k, v in payload.items() if k not in ("digest", "items")}
    rebuilt["digest"] = digest
    rebuilt["items"] = payload.get("items", [])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rebuilt, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"\n書き戻しました: {os.path.relpath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
