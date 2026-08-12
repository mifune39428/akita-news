"""
記事生成に使うLLMプロバイダの呼び分けと、フォールバック処理。

Gemini（無料枠あり）を第一候補とし、クォータ切れや未設定の場合は
Groq（無料枠あり）→ Claude（Anthropic）→ OpenAI の順に自動でフォールバックする。
Geminiが障害で落ちてもGroqだけで記事生成を完走できるよう、
無料で使えるプロバイダを2つ並べてある。

どのプロバイダを使うかはAPIキーが設定されているかどうかで決まるため、
使いたいプロバイダのキーだけ .env に書けばよい。

優先順位は LLM_PROVIDER_ORDER 環境変数（カンマ区切り）で変更できる。
    例: LLM_PROVIDER_ORDER=groq,gemini
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_PROVIDER_ORDER = ["gemini", "groq", "claude", "openai"]

# Geminiの無料枠クォータはモデルごとに独立して割り当てられるため、
# 先頭のモデルが1日の上限に達した場合は次のモデルへ自動的に切り替える。
# 2026-08-12に実際に疎通確認して並べ替えた。2.0系は提供終了（404）、
# 2.5-flash-lite は新規ユーザーには開放されていないため候補から外している。
GEMINI_MODEL_FALLBACKS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
]

# Groqはホストするモデルの入れ替えが早く、古いモデルIDは廃止される。
# 廃止・不明なモデルIDだった場合は次の候補へ自動的に切り替える。
#
# 日本語記事の品質を実際に比較して決めた順序（2026-07-30）:
#   - openai/gpt-oss-120b: 本文の長さ・ハッシュタグ形式ともに実用レベル。
#   - llama-3.3-70b-versatile: 本文が極端に短く、ハッシュタグが「# タグ」と
#     空白入りになって壊れる。品質は落ちるが最後の砦として残す。
#   - qwen/qwen3.6-27b は出力形式が崩壊してパースできないため採用しない。
GROQ_MODEL_FALLBACKS = [
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

DEFAULT_CLAUDE_MODEL = "claude-opus-5"
DEFAULT_OPENAI_MODEL = "gpt-4o"


class LLMError(RuntimeError):
    """記事生成用のLLM呼び出しに失敗した場合の例外。"""


class QuotaExceeded(LLMError):
    """そのプロバイダ／モデルの利用上限に達している場合の例外。"""


class ProviderUnavailable(LLMError):
    """そのプロバイダが今は使えない場合の例外。

    サーバー障害（5xx）、ネットワーク断、APIキー不正など、
    「待っても今すぐには直らないが、他社なら通る」種類の失敗を表す。
    利用上限と同じく、次のプロバイダへの切り替え理由になる。
    """


class ResponseInvalid(LLMError):
    """応答は返ってきたが、期待した形式になっていない場合の例外。

    指定したタグが欠けている、途中で打ち切られている等。
    同じプロバイダでの再試行や、他プロバイダへの切り替えで直ることが多い。
    """


class ContentRefused(LLMError):
    """メモの内容自体が原因で生成を拒否された場合の例外。

    他社に切り替えても同じ結果になるため、フォールバックせずに止める。
    """


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------

def _gemini_model_candidates() -> list[str]:
    preferred = (os.getenv("GEMINI_MODEL") or "").strip()
    models = list(GEMINI_MODEL_FALLBACKS)
    if preferred:
        models = [preferred] + [m for m in models if m != preferred]
    return models


def _is_daily_quota_error(detail: str) -> bool:
    """429の内容が「1日あたりの上限」によるものかを判定する。

    分単位の上限であれば待てば回復するが、1日あたりの上限の場合は
    待っても回復しないため、別モデル／別プロバイダへ切り替える必要がある。
    """
    return "PerDay" in detail


def _call_gemini_model(prompt: str, api_key: str, model: str) -> str:
    """単一のGeminiモデルにリクエストする。分単位の制限のみリトライする。"""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "topP": 0.95},
    }

    max_retries = 3
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                candidate = res_data["candidates"][0]
                if candidate.get("finishReason") == "MAX_TOKENS":
                    raise ResponseInvalid(
                        f"gemini/{model} の応答がトークン上限で打ち切られました。"
                    )
                # 思考するモデル（Gemini 3系）は parts の先頭に thought が入り、
                # parts[0]["text"] だと KeyError になる。text を持つ部分だけ連結する。
                parts = candidate.get("content", {}).get("parts", [])
                text = "".join(
                    p["text"] for p in parts if p.get("text") and not p.get("thought")
                )
                if not text.strip():
                    raise ResponseInvalid(f"gemini/{model} の応答が空です。")
                return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            last_error = e
            if e.code == 429:
                if _is_daily_quota_error(detail) or attempt == max_retries:
                    raise QuotaExceeded(f"gemini/{model}") from e
                time.sleep(attempt * 15)
                continue
            if e.code in (500, 502, 503, 504):
                # Gemini側の障害。リトライで戻らなければ他社へ切り替える。
                if attempt < max_retries:
                    time.sleep(attempt * 15)
                    continue
                raise ProviderUnavailable(
                    f"gemini/{model} (サーバー障害 {e.code})"
                ) from e
            raise ProviderUnavailable(
                f"gemini/{model} (APIエラー {e.code}: {detail[:200]})"
            ) from e
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < max_retries:
                time.sleep(3)
                continue
    raise ProviderUnavailable(f"gemini/{model} (接続失敗: {last_error})")


def call_gemini(prompt: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise QuotaExceeded("gemini (APIキー未設定)")

    # 上限も過負荷（503）もモデル単位で発生するため、どちらの場合も次のモデルを試す。
    exhausted: list[str] = []
    last_reason = ""
    for model in _gemini_model_candidates():
        try:
            if exhausted:
                print(f"  （Gemini {model} で再試行します）")
            return _call_gemini_model(prompt, api_key, model)
        except (QuotaExceeded, ProviderUnavailable) as exc:
            exhausted.append(model)
            last_reason = str(exc)
            continue
    raise ProviderUnavailable(
        f"gemini (全モデルが利用不可: {', '.join(exhausted)} / 最後の理由: {last_reason})"
    )


# --------------------------------------------------------------------------
# Groq（無料枠あり。OpenAI互換のHTTP APIなので追加パッケージ不要）
# --------------------------------------------------------------------------

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# GroqはCloudflare配下にあり、urllib既定の User-Agent（Python-urllib/x.y）は
# 403 "error code: 1010"（ブラウザ署名によるブロック）で弾かれる。
# 明示的にUser-Agentを送れば通るため、必ず付ける。
GROQ_USER_AGENT = "dual-draft-poster/1.0"


def _groq_model_candidates() -> list[str]:
    preferred = (os.getenv("GROQ_MODEL") or "").strip()
    models = list(GROQ_MODEL_FALLBACKS)
    if preferred:
        models = [preferred] + [m for m in models if m != preferred]
    return models


def _is_groq_model_gone(detail: str) -> bool:
    """モデルIDが廃止済み／存在しない場合か（＝別モデルなら成功しうる）。"""
    lowered = detail.lower()
    return "decommission" in lowered or "does not exist" in lowered or "model_not_found" in lowered


def _call_groq_model(prompt: str, api_key: str, model: str) -> str:
    data = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "top_p": 0.95,
        # 記事本文＋各種タグを収めるのに十分な長さを明示的に確保する。
        # 既定値のままだと応答が途中で打ち切られ、閉じタグが欠けることがある。
        "max_tokens": 16000,
    }

    max_retries = 3
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            GROQ_ENDPOINT,
            data=json.dumps(data).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": GROQ_USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                choice = res_data["choices"][0]
                text = choice["message"].get("content") or ""
                if not text.strip():
                    raise ResponseInvalid(f"groq/{model} の応答が空でした。")
                if choice.get("finish_reason") == "length":
                    raise ResponseInvalid(
                        f"groq/{model} の応答がトークン上限で打ち切られました。"
                    )
                return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            last_error = e
            # 廃止モデルは400/404で返るが、別モデルなら通るので上位で切り替える。
            if e.code in (400, 404) and _is_groq_model_gone(detail):
                raise QuotaExceeded(f"groq/{model} (モデル廃止)") from e
            if e.code == 429:
                # 1日あたりの上限は待っても回復しないため即座に切り替える。
                if "per day" in detail.lower() or attempt == max_retries:
                    raise QuotaExceeded(f"groq/{model}") from e
                time.sleep(attempt * 15)
                continue
            if e.code in (500, 502, 503, 504):
                # Groq側の障害。リトライで戻らなければ他社へ切り替える。
                if attempt < max_retries:
                    time.sleep(attempt * 15)
                    continue
                raise ProviderUnavailable(
                    f"groq/{model} (サーバー障害 {e.code})"
                ) from e
            raise ProviderUnavailable(
                f"groq/{model} (APIエラー {e.code}: {detail[:200]})"
            ) from e
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < max_retries:
                time.sleep(3)
                continue
    raise ProviderUnavailable(f"groq/{model} (接続失敗: {last_error})")


def call_groq(prompt: str) -> str:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise QuotaExceeded("groq (APIキー未設定)")

    # 上限も過負荷（5xx）もモデル単位で発生するため、どちらの場合も次のモデルを試す。
    exhausted: list[str] = []
    last_reason = ""
    for model in _groq_model_candidates():
        try:
            if exhausted:
                print(f"  （Groq {model} で再試行します）")
            return _call_groq_model(prompt, api_key, model)
        except (QuotaExceeded, ProviderUnavailable) as exc:
            exhausted.append(model)
            last_reason = str(exc)
            continue
    raise ProviderUnavailable(
        f"groq (全モデルが利用不可: {', '.join(exhausted)} / 最後の理由: {last_reason})"
    )


# --------------------------------------------------------------------------
# Claude（Anthropic）
# --------------------------------------------------------------------------

def call_claude(prompt: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise QuotaExceeded("claude (APIキー未設定)")

    try:
        import anthropic
    except ImportError as exc:
        raise LLMError(
            "anthropicパッケージが見つかりません。"
            "`pip install anthropic` を実行してください。"
        ) from exc

    model = (os.getenv("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL).strip()
    client = anthropic.Anthropic(api_key=api_key)

    try:
        # 長文生成のためストリーミングを使う（HTTPタイムアウト回避）。
        # fallbacks="default" は、安全性分類器がリクエストを拒否した場合に
        # Anthropic側が推奨する別モデルで自動的に再実行する仕組み。
        with client.beta.messages.stream(
            model=model,
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.RateLimitError as exc:
        raise QuotaExceeded(f"claude/{model} (レート制限)") from exc
    except anthropic.APIStatusError as exc:
        if exc.status_code in (402, 429):
            raise QuotaExceeded(f"claude/{model} (利用上限)") from exc
        # クレジット残高不足は 400 invalid_request_error として返ってくるが、
        # 「このプロバイダは今使えない」という意味なので次のプロバイダへ回す。
        if "credit balance" in str(exc).lower():
            raise QuotaExceeded(
                f"claude/{model} (クレジット残高不足。"
                "Anthropic Consoleの Plans & Billing でクレジットを購入してください)"
            ) from exc
        raise LLMError(f"Claude APIエラー ({exc.status_code}): {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"Claude API呼び出しに失敗しました: {exc}") from exc

    if message.stop_reason == "refusal":
        # 他社に切り替えても同じ結果になるため、フォールバックせずに止める。
        raise ContentRefused(
            "Claudeがこの内容の生成を拒否しました。メモの内容を見直してください。"
        )

    text = "".join(b.text for b in message.content if b.type == "text")
    if not text.strip():
        raise LLMError("Claudeの応答が空でした。")
    return text


# --------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------

def call_openai(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise QuotaExceeded("openai (APIキー未設定)")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMError(
            "openaiパッケージが見つかりません。"
            "`pip install openai` を実行してください。"
        ) from exc

    model = (os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL).strip()
    client = OpenAI(api_key=api_key)

    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", None)
        if status in (402, 429):
            raise QuotaExceeded(f"openai/{model} (利用上限)") from exc
        raise LLMError(f"OpenAI API呼び出しに失敗しました: {exc}") from exc

    text = completion.choices[0].message.content or ""
    if not text.strip():
        raise LLMError("OpenAIの応答が空でした。")
    return text


# --------------------------------------------------------------------------
# フォールバック付きの呼び出し
# --------------------------------------------------------------------------

def call_browser(prompt: str) -> str:
    """ブラウザのChatGPT等に手貼りして、応答をクリップボード経由で受け取る。

    APIキーを使わない代わりに人の操作が2回だけ入る。既定の優先順位には
    含まれないので、使うときは LLM_PROVIDER_ORDER=browser を指定する。
    """
    # browser_writer 側が本モジュールの例外クラスを使うため、遅延importで循環を避ける。
    from browser_writer import call_browser as _call_browser

    return _call_browser(prompt)


PROVIDERS = {
    "gemini": ("Gemini", call_gemini),
    "groq": ("Groq", call_groq),
    "claude": ("Claude", call_claude),
    "openai": ("OpenAI", call_openai),
    "browser": ("ブラウザ手貼り", call_browser),
}


def _provider_order() -> list[str]:
    raw = (os.getenv("LLM_PROVIDER_ORDER") or "").strip()
    if not raw:
        return list(DEFAULT_PROVIDER_ORDER)
    order = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return [p for p in order if p in PROVIDERS] or list(DEFAULT_PROVIDER_ORDER)


def generate_text(prompt: str, validate=None, attempts_per_provider: int = 2) -> str:
    """設定された優先順位でプロバイダを試し、最初に成功した応答を返す。

    APIキー未設定・クォータ切れ・サーバー障害のプロバイダは自動的にスキップされる。
    メモの内容自体が拒否された場合（ContentRefused）だけは、
    他社でも同じ結果になるためフォールバックせずにそのまま投げる。

    validate に関数を渡すと、応答テキストを受け取って検証させられる。
    形式が不正なら ResponseInvalid を投げること。その場合は同じプロバイダで
    もう一度試し、それでもダメなら次のプロバイダへ回す。
    LLMの応答は同じ入力でも揺れるため、1回の形式崩れで全体を止めないための仕組み。
    """
    skipped: list[str] = []

    for key in _provider_order():
        label, func = PROVIDERS[key]
        if skipped:
            print(f"=== {label} に切り替えて再試行します ===")

        for attempt in range(1, attempts_per_provider + 1):
            try:
                text = func(prompt)
                if validate is not None:
                    validate(text)
                return text
            except ContentRefused:
                raise
            except ResponseInvalid as exc:
                # 形式崩れは揺れによるものが多いので、同じプロバイダで振り直す。
                if attempt < attempts_per_provider:
                    print(f"  （{label} の応答が不正: {exc} → 生成し直します）")
                    continue
                print(f"  （{label} は使えませんでした: {exc}）")
                skipped.append(f"{label}: {exc}")
            except LLMError as exc:
                print(f"  （{label} は使えませんでした: {exc}）")
                skipped.append(f"{label}: {exc}")
            break

    raise LLMError(
        "記事を生成できるLLMがありません。\n"
        + "\n".join(f"  - {s}" for s in skipped)
        + "\n\n.env に GEMINI_API_KEY / GROQ_API_KEY / ANTHROPIC_API_KEY / "
        "OPENAI_API_KEY のいずれかを設定してください。\n"
        "GeminiとGroqの無料枠はモデルごとに1日あたりの回数制限があり、"
        "日付が変わるとリセットされます。"
    )
