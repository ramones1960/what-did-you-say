"""ローカル LLM 連携(要約・議事録の生成)。

Ollama / LM Studio / llama.cpp server / vLLM など、OpenAI 互換の
chat/completions エンドポイントを持つローカル LLM サーバーに接続する。
LLM_API_URL(例: http://localhost:11434/v1)が未設定なら機能ごと無効で、
UI にもボタンが表示されない。「完全ローカル」の前提を守るため、既定では
どこにも接続しない。外部の SaaS を指定しないこと。

generate() はブロッキング(urllib)なので、呼び出し側で asyncio.to_thread
などを使ってイベントループの外で実行すること。生成結果の保存は呼び出し側
(main.py → db.save_summary)が行う。
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from . import config, exporters

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM サーバーへの接続・生成の失敗(ユーザーに見せられるメッセージを持つ)。"""


# 生成の種類 → (表示名, システムプロンプト)
KINDS: dict[str, tuple[str, str]] = {
    "summary": (
        "要約",
        "あなたは会議・会話の文字起こしを要約するアシスタントです。"
        "与えられた文字起こしを日本語で簡潔に要約してください。"
        "重要な論点・決定事項・数値は落とさず、箇条書きを中心に整理すること。"
        "文字起こしに含まれない情報を補わないこと。",
    ),
    "minutes": (
        "議事録",
        "あなたは会議の文字起こしから議事録を作成するアシスタントです。"
        "与えられた文字起こしをもとに、日本語で Markdown 形式の議事録を作成してください。"
        "構成: ## 概要 / ## 参加者 / ## 議論の要点 / ## 決定事項 / ## TODO。"
        "参加者は話者ラベル(または設定済みの氏名)から列挙すること。"
        "要点には [HH:MM:SS] 形式の時刻タグを適宜引用すること。"
        "決定事項・TODO が文字起こしから読み取れない場合は「なし」と書き、推測で補わないこと。",
    ),
}


def enabled() -> bool:
    """LLM 連携が設定されているか(未設定なら API・UI とも無効)。"""
    return bool(config.LLM_API_URL)


def build_transcript(segments: list[dict[str, Any]], names: dict[str, str] | None) -> str:
    """セグメント一覧を LLM 入力用のテキストに整形する。

    トークンを節約するため「長い」粒度で結合し、話者氏名を反映した
    時刻タグ付きテキストにする。長すぎる場合は先頭から LLM_MAX_INPUT_CHARS
    で切り詰め、切り詰めた旨を末尾に付記する。
    """
    merged = exporters.merge_segments(segments, "long")
    text = exporters.to_txt(merged, names)
    if len(text) > config.LLM_MAX_INPUT_CHARS:
        text = (
            text[: config.LLM_MAX_INPUT_CHARS]
            + "\n(注: 文字数上限のため、文字起こしはここまでで打ち切られています)\n"
        )
    return text


def generate(
    kind: str, segments: list[dict[str, Any]], names: dict[str, str] | None
) -> str:
    """文字起こしから kind(summary / minutes)のテキストを生成して返す。

    ブロッキング呼び出し。失敗時はユーザー向けメッセージ付きの LLMError を投げる。
    """
    if not enabled():
        raise LLMError("LLM 連携が設定されていません(LLM_API_URL を設定してください)")
    if kind not in KINDS:
        raise LLMError(f"未対応の生成種類です: {kind}")
    if not segments:
        raise LLMError("文字起こし結果が空のため生成できません")

    _label, system_prompt = KINDS[kind]
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_transcript(segments, names)},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    request = urllib.request.Request(
        f"{config.LLM_API_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.LLM_TIMEOUT) as res:
            body = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        logger.error("LLM API error %s: %s", e.code, detail)
        raise LLMError(
            f"LLM サーバーがエラーを返しました (HTTP {e.code})。モデル名 ({config.LLM_MODEL}) が正しいか確認してください"
        ) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.error("LLM API unreachable: %s", e)
        raise LLMError(
            "LLM サーバーに接続できませんでした。LLM_API_URL の設定とサーバーの起動状態を確認してください"
        ) from e

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("LLM サーバーの応答形式が不正です") from e
    content = (content or "").strip()
    if not content:
        raise LLMError("LLM が空の応答を返しました")
    return content
