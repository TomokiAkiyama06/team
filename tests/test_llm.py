from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from classbot.llm import FALLBACK_BETA, TRUNCATED_NOTE, ClaudeClient, LLMError

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def status_err(cls, code):
    return cls("err", response=httpx2.Response(code, request=REQ), body=None)


def make_resp(text="回答です[S1]\n[[SOURCES: S1]]", stop="end_turn", model="claude-opus-5-5"):
    r = SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop, model=model,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50,
                              cache_creation_input_tokens=None, cache_read_input_tokens=20))
    r._request_id = "req_abc"
    return r


class FakeMessages:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result):
        self.messages = FakeMessages(result)
        self.beta = SimpleNamespace(messages=FakeMessages(result))


@pytest.mark.asyncio
async def test_fallback_request_shape(settings):
    settings.claude_refusal_fallback = True
    fc = FakeClient(make_resp())
    resp = await ClaudeClient(settings, fc).answer("SYS", [{"role": "user", "content": "q"}])
    kw = fc.beta.messages.calls[0]
    assert kw["model"] == settings.claude_model
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["output_config"] == {"effort": settings.claude_effort}
    assert "thinking" not in kw
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert resp.text.startswith("回答です") and resp.request_id == "req_abc"
    assert resp.usage.cache_read_input_tokens == 20 and resp.usage.cache_creation_input_tokens == 0


@pytest.mark.asyncio
async def test_no_fallback_no_cache(settings):
    settings.claude_refusal_fallback = False
    settings.claude_prompt_cache = False
    fc = FakeClient(make_resp())
    await ClaudeClient(settings, fc).answer("SYS", [])
    kw = fc.messages.calls[0]
    assert "betas" not in kw and "cache_control" not in kw["system"][0]


@pytest.mark.asyncio
async def test_truncated_and_refusal(settings):
    r = await ClaudeClient(settings, FakeClient(make_resp(stop="max_tokens"))).answer("S", [])
    assert r.truncated and r.notice == TRUNCATED_NOTE
    r = await ClaudeClient(settings, FakeClient(make_resp(stop="refusal"))).answer("S", [])
    assert r.refused and "お答えできません" in r.text and r.usage.input_tokens == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,kind,billed", [
    (status_err(anthropic.RateLimitError, 429), "rate_limit", False),
    (anthropic.APITimeoutError(request=REQ), "timeout", True),
    (anthropic.APIConnectionError(request=REQ), "connection", False),
    (status_err(anthropic.AuthenticationError, 401), "auth", False),
    (status_err(anthropic.BadRequestError, 400), "bad_request", False),
    (status_err(anthropic.InternalServerError, 500), "server", False),
])
async def test_error_mapping(settings, exc, kind, billed):
    with pytest.raises(LLMError) as ei:
        await ClaudeClient(settings, FakeClient(exc)).answer("S", [])
    assert ei.value.kind == kind and ei.value.billed_unknown is billed
    assert ei.value.user_message and "sk-ant" not in ei.value.user_message


async def test_fallback_disabled_by_default(settings):
    from classbot.config import Settings

    assert Settings(_env_file=None).claude_refusal_fallback is False
    fc = FakeClient(make_resp())
    await ClaudeClient(settings, fc).answer("S", [])
    assert fc.messages.calls and not fc.beta.messages.calls


async def test_fallback_not_sent_for_unsupported_model(settings):
    settings.claude_refusal_fallback = True
    settings.claude_model = "claude-haiku-5-5"  # サーバー側フォールバック非対応
    fc = FakeClient(make_resp())
    client = ClaudeClient(settings, fc)
    assert client.use_fallback is False
    await client.answer("S", [])
    assert "betas" not in fc.messages.calls[0] and not fc.beta.messages.calls


async def test_fallback_iterations_are_summed_per_model(settings):
    settings.claude_refusal_fallback = True
    resp = make_resp(model="claude-sonnet-5")
    it = lambda t, m, i, o: SimpleNamespace(type=t, model=m, input_tokens=i, output_tokens=o,  # noqa: E731
                                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
    resp.usage.iterations = [it("message", "claude-sonnet-5-5", 500, 40),
                             it("fallback_message", "claude-sonnet-5", 400, 200)]
    r = await ClaudeClient(settings, FakeClient(resp)).answer("S", [])
    assert [m for m, _ in r.iterations] == ["claude-sonnet-5-5", "claude-sonnet-5"]
    assert r.usage.input_tokens == 900 and r.usage.output_tokens == 240
    assert r.model == "claude-sonnet-5"


def _real_sdk_client(captured):
    """実 SDK でリクエストを組み立て、送信直前で捕まえる（ネットワークに出ない）。"""
    import json as _json

    def handler(request):
        captured.append((request, _json.loads(request.content)))
        body = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 2, "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0}}
        return httpx2.Response(200, json=body, headers={"request-id": "req_x"})

    return anthropic.AsyncAnthropic(
        api_key="test-key", max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))


@pytest.mark.parametrize("fallback", [False, True])
async def test_real_sdk_serialization(settings, fallback):
    settings.claude_model = "claude-sonnet-5-5"
    settings.claude_refusal_fallback = fallback
    captured = []
    r = await ClaudeClient(settings, _real_sdk_client(captured)).answer(
        "SYS", [{"role": "user", "content": "質問"}])
    req, body = captured[0]
    assert body["model"] == "claude-sonnet-5-5" and "thinking" not in body
    assert body["output_config"] == {"effort": settings.claude_effort}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    if fallback:
        assert body["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in req.headers.get("anthropic-beta", "")
    else:
        assert "fallbacks" not in body and "anthropic-beta" not in req.headers
    assert r.text == "ok" and r.request_id == "req_x" and r.iterations[0][0] == "claude-sonnet-5-5"
