"""Model backend adapters — request mapping / response parsing over HTTP mocks."""

import json

import httpx
import pytest

from agent_augury.backend.fake import FakeModelBackend
from agent_augury.backend.openai_compat import OpenAICompatBackend


def make_backend(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatBackend(
        base_url="http://fake.local/v1", api_key="secret-key", model="test-model", client=client
    )


# ---------------------------------------------------------------------------
# request mapping
# ---------------------------------------------------------------------------


async def test_request_carries_model_messages_and_auth_header():
    recorded = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded["payload"] = json.loads(request.content)
        recorded["auth"] = request.headers.get("authorization")
        recorded["url"] = str(request.url)
        return httpx.Response(200, json=_chat_response(text="hi"))

    backend = make_backend(handler)
    await backend.complete(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}], tools=[]
    )

    assert recorded["url"].startswith("http://fake.local/v1")
    assert recorded["auth"] == "Bearer secret-key"
    assert recorded["payload"]["model"] == "test-model"
    assert recorded["payload"]["messages"][0]["role"] == "system"


async def test_internal_tool_spec_maps_to_openai_function_schema():
    recorded = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_response(text="ok"))

    backend = make_backend(handler)
    tools = [
        {
            "name": "send_message",
            "description": "post a msg",
            "schema": {"type": "object", "properties": {"content": {"type": "string"}}},
        }
    ]
    await backend.complete([], tools)

    mapped = recorded["payload"]["tools"]
    assert mapped[0]["type"] == "function"
    fn = mapped[0]["function"]
    assert fn["name"] == "send_message"
    assert fn["description"] == "post a msg"
    assert fn["parameters"] == tools[0]["schema"]


# ---------------------------------------------------------------------------
# response parsing
# ---------------------------------------------------------------------------


async def test_text_response_parsed():
    backend = make_backend(lambda req: httpx.Response(200, json=_chat_response(text="hello")))
    completion = await backend.complete([], [])
    assert completion.text == "hello"
    assert completion.tool_calls == []


async def test_tool_call_response_parses_json_arguments():
    raw_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "create_thread", "arguments": '{"name": "plan", "participants": ["agent-1"]}'},
        }
    ]
    backend = make_backend(
        lambda req: httpx.Response(200, json=_chat_response(tool_calls=raw_calls))
    )
    completion = await backend.complete([], [])
    assert completion.text is None
    assert len(completion.tool_calls) == 1
    call = completion.tool_calls[0]
    assert call.name == "create_thread"
    assert call.arguments == {"name": "plan", "participants": ["agent-1"]}
    assert call.id == "call_1"


async def test_http_error_returns_error_text():
    """HTTP errors (4xx/5xx) must return error text, not raise."""
    def handler(request):
        return httpx.Response(404, json={"error": "Not Found"})

    backend = make_backend(handler)
    completion = await backend.complete([], [])
    assert completion.text is not None
    assert "[backend error]" in completion.text
    assert "404" in completion.text
    assert "Not Found" in completion.text


async def test_network_error_returns_error_text():
    """Network errors (DNS, connection refused) must return error text, not raise."""
    def handler(request):
        raise httpx.RequestError("connection refused")

    backend = make_backend(handler)
    completion = await backend.complete([], [])
    assert completion.text is not None
    assert "[backend error]" in completion.text
    assert "connection refused" in completion.text


# ---------------------------------------------------------------------------
# fake + portal
# ---------------------------------------------------------------------------


async def test_fake_backend_replays_script_and_records_calls():
    from agent_augury.backend.base import Completion

    fake = FakeModelBackend([Completion(text="one"), Completion(text="two")])
    assert (await fake.complete([], [])).text == "one"
    assert (await fake.complete([], [])).text == "two"
    assert fake.call_count == 2


def test_nous_portal_requires_explicit_base_url():
    from agent_augury.backend.nous_portal import NousPortalBackend

    with pytest.raises(ValueError):
        NousPortalBackend(api_key="k", model="m")  # D4: spec unconfirmed — no invented default


def _chat_response(*, text=None, tool_calls=None):
    message: dict = {}
    if text is not None:
        message["content"] = text
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
        message.setdefault("content", None)
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    }
