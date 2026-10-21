"""Claude API 呼び出し（Anthropic 公式 Python SDK・非同期）。

- モデル名・effort・max_tokens などはすべて設定から読む（ソースに固定しない）。
- タイムアウト・リトライ（指数バックオフ、429/5xx/接続エラー）は SDK の機能を使う。
- 失敗時は利用者向けの日本語メッセージを持つ LLMError を送出する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic

from classbot.budget import Usage
from classbot.config import Settings

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# fallbacks="default"（beta、Claude API のみ）を受け付けるモデル。Claude Haiku 5.5 などは
# サーバー側フォールバックが無く 400 になるため、ここに無いモデルでは設定が有効でも送らない。
FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})
TRUNCATED_NOTE = "（回答が長くなったため途中で終了しました。質問を分けて再度お試しください。）"
REFUSAL_MESSAGE = ("申し訳ありませんが、この質問にはお答えできませんでした。"
                   "質問の表現を変えるか、授業担当者に直接お問い合わせください。")


@dataclass
class LLMResponse:
    text: str                  # テキストブロックを連結した回答本文（thinking 等は含まない）
    stop_reason: str | None
    model: str | None          # 実際に応答したモデル（フォールバック時は設定と異なる場合がある）
    usage: Usage
    request_id: str | None
    truncated: bool = False
    refused: bool = False
    notice: str | None = None  # 回答末尾に付ける注記（途中終了など）
    # 課金対象の試行ごとの (モデル, 使用量)。フォールバック時は拒否した試行と代替モデルの試行が
    # 別々に入る（top-level usage は最後の試行分だけなので、費用計算はこちらを使う）。
    iterations: list[tuple[str | None, Usage]] = field(default_factory=list)


class LLMError(Exception):
    def __init__(self, kind: str, user_message: str, *, billed_unknown: bool = False,
                 detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.user_message = user_message
        self.billed_unknown = billed_unknown   # True: 課金されたか不明（予約額で計上する）
        self.detail = detail


def _tokens(u: Any) -> Usage:
    return Usage(
        input_tokens=int(getattr(u, "input_tokens", 0) or 0),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
        cache_creation_input_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        cache_read_input_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
    )


def _usage_from(resp: Any) -> Usage:
    u = getattr(resp, "usage", None)
    return Usage() if u is None else _tokens(u)


def _iterations_from(resp: Any) -> list[tuple[str | None, Usage]]:
    """usage.iterations（フォールバック時の試行ごとの記録）を (モデル, 使用量) の列にする。
    無ければ top-level usage を応答モデル1件として返す。"""
    u = getattr(resp, "usage", None)
    items = getattr(u, "iterations", None) if u is not None else None
    if items:
        return [(getattr(it, "model", None), _tokens(it)) for it in items]
    return [(getattr(resp, "model", None), _usage_from(resp))]


def map_error(e: Exception) -> LLMError:
    """SDK の例外を利用者向けメッセージ付きの LLMError に変換する（具体的な型から順に判定）。"""
    detail = type(e).__name__
    if isinstance(e, anthropic.APITimeoutError):
        return LLMError("timeout", "AI の応答に時間がかかりすぎたため中断しました。"
                        "少し時間をおいて再度お試しください。", billed_unknown=True, detail=detail)
    if isinstance(e, anthropic.APIConnectionError):
        return LLMError("connection", "AI サービスに接続できませんでした。"
                        "少し時間をおいて再度お試しください。", detail=detail)
    if isinstance(e, anthropic.RateLimitError):
        return LLMError("rate_limit", "現在 AI サービスが混雑しています。"
                        "しばらく待ってから再度お試しください。", detail=detail)
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMError("auth", "ボットの設定に問題があるため回答できませんでした。"
                        "授業担当者（管理者）に連絡してください。", detail=detail)
    if isinstance(e, anthropic.NotFoundError):
        return LLMError("not_found", "ボットの設定（モデル名）に問題があるため回答できませんでした。"
                        "授業担当者（管理者）に連絡してください。", detail=detail)
    if isinstance(e, anthropic.BadRequestError):
        return LLMError("bad_request", "リクエストを処理できませんでした。"
                        "質問を短くするか、新しいスレッドで質問し直してください。",
                        detail=f"{detail}: {getattr(e, 'message', '')[:300]}")
    if isinstance(e, anthropic.APIStatusError):
        return LLMError("server", "AI サービスが一時的に利用できません。"
                        "少し時間をおいて再度お試しください。",
                        billed_unknown=False, detail=f"{detail} status={e.status_code}")
    return LLMError("unknown", "回答の生成中にエラーが発生しました。少し時間をおいて再度お試しください。",
                    billed_unknown=True, detail=detail)


class ClaudeClient:
    def __init__(self, settings: Settings, client: Any | None = None):
        self.settings = settings
        self.use_fallback = settings.claude_refusal_fallback and settings.claude_model in FALLBACK_MODELS
        if settings.claude_refusal_fallback and not self.use_fallback:
            log.warning("refusal fallback is not supported for this model; disabled",
                        extra={"model": settings.claude_model})
        self.client = client or anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.claude_timeout_sec,
            max_retries=settings.claude_max_retries,
        )

    def _request_kwargs(self, system: str, messages: list[dict]) -> dict[str, Any]:
        s = self.settings
        system_block: dict[str, Any] = {"type": "text", "text": system}
        if s.claude_prompt_cache:
            system_block["cache_control"] = {"type": "ephemeral"}
        return {
            "model": s.claude_model,
            "max_tokens": s.claude_max_tokens,
            "system": [system_block],
            "messages": messages,
            "output_config": {"effort": s.claude_effort},
        }

    async def answer(self, system: str, messages: list[dict]) -> LLMResponse:
        kwargs = self._request_kwargs(system, messages)
        try:
            if self.use_fallback:
                resp = await self.client.beta.messages.create(
                    **kwargs, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                resp = await self.client.messages.create(**kwargs)
        except anthropic.APIError as e:
            err = map_error(e)
            log.error("claude_api_error", extra={"kind": err.kind, "detail": err.detail})
            raise err from e

        iterations = _iterations_from(resp)
        usage = Usage(*(sum(getattr(u, f) for _, u in iterations) for f in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
        request_id = getattr(resp, "_request_id", None)
        stop = getattr(resp, "stop_reason", None)
        model = getattr(resp, "model", None)
        log.info("claude_response", extra={
            "model": model, "stop_reason": stop, "request_id": request_id,
            "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
        })

        if stop == "refusal":
            return LLMResponse(text=REFUSAL_MESSAGE, stop_reason=stop, model=model, usage=usage,
                               request_id=request_id, refused=True, iterations=iterations)

        text = "".join(getattr(b, "text", "") for b in (resp.content or [])
                       if getattr(b, "type", None) == "text").strip()
        truncated = stop == "max_tokens"
        if not text:
            text = "回答を生成できませんでした。質問を変えて再度お試しください。"
        return LLMResponse(text=text, stop_reason=stop, model=model, usage=usage,
                           request_id=request_id, truncated=truncated,
                           notice=TRUNCATED_NOTE if truncated else None, iterations=iterations)

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            await close()
