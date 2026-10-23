"""Anthropic Messages API adapter — request mapping / response parsing."""

from __future__ import annotations

import json

import httpx

from agent_augury.backend.anthropic import (
    ANTHROPIC_API_VERSION,
    AnthropicBackend,
    _to_anthropic_messages,
)


def make_backend(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AnthropicBackend(
        base_url="http://fake.local",
        api_key="secret-key",
        model="claude-test",
        client=client,
    )


def _messages_response(*, text=None, tool_uses=None, usage=None):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        content.append(
            {
                "type": "tool_use",
                "id": tu["id"],
                "name": tu["name"],
                "input": tu.get("input") or {},
            }
        )
    body = {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": "claude-test",
        "stop_reason": "end_turn",
    }
    if usage is not None:
        body["usage"] = usage
    return body


async def test_request_carries_model_system_and_anthropic_headers():
    recorded = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded["payload"] = json.loads(request.content)
        recorded["headers"] = dict(request.headers)
        recorded["url"] = str(request.url)
        return httpx.Response(200, json=_messages_response(text="hi"))

    backend = make_backend(handler)
    await backend.complete(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        tools=[],
    )

    assert recorded["url"] == "http://fake.local/v1/messages"
    assert recorded["headers"].get("x-api-key") == "secret-key"
    assert recorded["headers"].get("anthropic-version") == ANTHROPIC_API_VERSION
    assert recorded["payload"]["model"] == "claude-test"
    assert recorded["payload"]["max_tokens"] == 8192
    assert recorded["payload"]["system"] == "sys"
    assert recorded["payload"]["messages"][0] == {"role": "user", "content": "go"}
    assert "system" not in {m["role"] for m in recorded["payload"]["messages"]}


async def test_internal_tool_spec_maps_to_anthropic_input_schema():
    recorded = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_messages_response(text="ok"))

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
    assert mapped[0]["name"] == "send_message"
    assert mapped[0]["description"] == "post a msg"
    assert mapped[0]["input_schema"] == tools[0]["schema"]
    assert "type" not in mapped[0] or mapped[0].get("type") != "function"


async def test_tool_history_maps_to_tool_use_and_tool_result():
    system, msgs = _to_anthropic_messages(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "create_thread",
                            "arguments": '{"name": "plan"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
        ]
    )
    assert system == "s"
    assert msgs[0] == {"role": "user", "content": "go"}
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[1]["content"][0]["id"] == "call_1"
    assert msgs[1]["content"][0]["input"] == {"name": "plan"}
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0]["type"] == "tool_result"
    assert msgs[2]["content"][0]["tool_use_id"] == "call_1"


async def test_text_response_parsed():
    backend = make_backend(
        lambda req: httpx.Response(200, json=_messages_response(text="hello"))
    )
    completion = await backend.complete([], [])
    assert completion.text == "hello"
    assert completion.tool_calls == []


async def test_tool_use_response_parsed():
    backend = make_backend(
        lambda req: httpx.Response(
            200,
            json=_messages_response(
                tool_uses=[
                    {
                        "id": "toolu_1",
                        "name": "create_thread",
                        "input": {"name": "plan", "participants": ["agent-1"]},
                    }
                ]
            ),
        )
    )
    completion = await backend.complete([], [])
    assert completion.text is None
    assert len(completion.tool_calls) == 1
    call = completion.tool_calls[0]
    assert call.id == "toolu_1"
    assert call.name == "create_thread"
    assert call.arguments == {"name": "plan", "participants": ["agent-1"]}


async def test_http_error_returns_structured_error():
    def handler(request):
        return httpx.Response(404, json={"error": {"message": "Not Found"}})

    backend = make_backend(handler)
    completion = await backend.complete([], [])
    assert completion.text is None
    assert completion.error is not None
    assert completion.error.kind == "model_not_found"
    assert completion.error.status == 404


async def test_network_error_returns_structured_error():
    def handler(request):
        raise httpx.RequestError("connection refused")

    backend = make_backend(handler)
    completion = await backend.complete([], [])
    assert completion.text is None
    assert completion.error is not None
    assert completion.error.kind == "network"
    assert completion.error.retryable is True


async def test_list_models_uses_anthropic_headers():
    recorded = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded["url"] = str(request.url)
        recorded["headers"] = dict(request.headers)
        return httpx.Response(
            200, json={"data": [{"id": "claude-sonnet-4-5", "type": "model"}]}
        )

    backend = make_backend(handler)
    models = await backend.list_models()
    assert models == ["claude-sonnet-4-5"]
    assert recorded["url"] == "http://fake.local/v1/models?limit=1000"
    assert recorded["headers"].get("x-api-key") == "secret-key"


def test_empty_turns_are_dropped():
    """Empty assistant/user content is a 400 on Anthropic when not final."""
    _, msgs = _to_anthropic_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": ""},
            {"role": "user", "content": "next"},
        ]
    )
    assert msgs == [
        {"role": "user", "content": "go"},
        {"role": "user", "content": "next"},
    ]


def test_trailing_assistant_gets_user_turn():
    """Ending on assistant would be read as prefill — always end on user."""
    _, msgs = _to_anthropic_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "done"},
        ]
    )
    assert msgs[-2] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
    }
    assert msgs[-1] == {"role": "user", "content": "(continue)"}


def test_system_only_history_still_sends_a_user_turn():
    system, msgs = _to_anthropic_messages([{"role": "system", "content": "s"}])
    assert system == "s"
    assert msgs == [{"role": "user", "content": "(continue)"}]


async def test_max_tokens_stop_is_logged(caplog):
    body = _messages_response(text="cut")
    body["stop_reason"] = "max_tokens"
    backend = make_backend(lambda req: httpx.Response(200, json=body))
    with caplog.at_level("WARNING", logger="agent_augury.backend.anthropic"):
        completion = await backend.complete([], [])
    assert completion.text == "cut"
    assert "max_tokens" in caplog.text
