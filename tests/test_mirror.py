"""v0.1b — read-only Discord observation mirror (D3 unblocks here).

Pull-based: subscribers enqueue, ``flush()`` posts to the Discord webhook.
The core NEVER reads anything back from Discord (§3.3 — channels are views).
"""

import httpx
import pytest

from agent_augury.channels.discord.mirror import DiscordWebhookMirror, mirror_from_config


def make_mirror(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DiscordWebhookMirror(
        webhook_url="https://discord.local/api/webhooks/1/secret", client=client
    )


async def test_flush_posts_formatted_content_to_webhook():
    recorded = []

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request.read())
        return httpx.Response(204)

    mirror = make_mirror(handler)
    mirror.enqueue(
        {
            "thread_id": "t1",
            "author": "agent-1",
            "content": "(URGENT) 정답은 43.",
        }
    )
    await mirror.flush()

    assert len(recorded) == 1
    body = recorded[0].decode("utf-8")
    assert '"content"' in body
    assert "agent-1" in body
    assert "(URGENT) 정답은 43." in body


async def test_flush_drains_outbox_and_is_idempotent():
    posts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.read())
        return httpx.Response(204)

    mirror = make_mirror(handler)
    mirror.enqueue({"thread_id": "t", "author": "a", "content": "m1"})
    mirror.enqueue({"thread_id": "t", "author": "b", "content": "m2"})
    await mirror.flush()
    assert len(posts) == 2
    await mirror.flush()  # outbox already drained
    assert len(posts) == 2


async def test_http_errors_are_swallowed_and_recorded():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    mirror = make_mirror(handler)
    mirror.enqueue({"thread_id": "t", "author": "a", "content": "m"})
    await mirror.flush()  # must NOT raise — observation must not kill the session
    assert len(mirror.errors) == 1


def test_missing_env_var_disables_mirror(monkeypatch):
    monkeypatch.delenv("AUGURY_NOWHERE", raising=False)
    assert mirror_from_config({"type": "discord_webhook", "url_env": "AUGURY_NOWHERE"}) is None


def test_unknown_mirror_type_raises():
    with pytest.raises(ValueError):
        mirror_from_config({"type": "carrier_pigeon"})


async def test_format_line_contains_thread_author_content():
    line = DiscordWebhookMirror.format_line(
        {"thread_id": "t9", "author": "agent-2", "content": "hello"}
    )
    assert "agent-2" in line and "hello" in line


async def test_format_line_chunks_long_content_on_enqueue():
    from agent_augury.channels.discord.mirror import _MAX_CONTENT

    long = "x" * (_MAX_CONTENT + 500)
    mirror = DiscordWebhookMirror(webhook_url="https://example.test/hook")
    mirror.enqueue({"thread_id": "t1", "author": "a1", "content": long})
    assert len(mirror.outbox) >= 2
    assert all(len(line) <= _MAX_CONTENT for line in mirror.outbox)
    assert f"(1/{len(mirror.outbox)})" in mirror.outbox[0]
    # Full content preserved across chunks (strip indicators).
    bodies = []
    for line in mirror.outbox:
        body = line
        if " (" in body and body.endswith(")"):
            body = body.rsplit(" (", 1)[0]
        # Drop mirror prefix on first chunk
        if "**: " in body:
            body = body.split("**: ", 1)[1]
        bodies.append(body)
    assert "x" * 100 in "".join(bodies)


async def test_flush_success_returns_sent_count():
    """When a real URL is provided and the webhook responds 204, flush succeeds."""
    posts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.read())
        return httpx.Response(204)

    mirror = make_mirror(handler)
    mirror.enqueue({"thread_id": "t1", "author": "a", "content": "msg1"})
    mirror.enqueue({"thread_id": "t2", "author": "b", "content": "msg2"})

    sent = await mirror.flush()
    assert sent == 2
    assert len(posts) == 2
    assert mirror.outbox == []  # drained
    assert mirror.errors == []  # no errors recorded


async def test_discord_webhook_integration_end_to_end():
    """Full enqueue → flush flow simulating a real Discord webhook.

    This test verifies the complete path:
    1. Server subscription receives a message
    2. Mirror enqueues it (formatting applied)
    3. flush() POSTs to the webhook URL
    4. Success is reported, outbox is drained
    """
    posts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        posts.append(body)
        # Discord webhooks return 204 No Content on success
        return httpx.Response(204)

    mirror = make_mirror(handler)

    # Simulate server subscription firing on a message
    test_message = {
        "thread_id": "thread-42",
        "author": "agent-1",
        "content": "PROPOSE: split the work evenly",
    }
    mirror.on_message(test_message)

    # Outbox should have the formatted line
    assert len(mirror.outbox) == 1
    assert "agent-1" in mirror.outbox[0]
    assert "PROPOSE: split the work evenly" in mirror.outbox[0]

    # Flush posts to the webhook
    sent = await mirror.flush()
    assert sent == 1
    assert len(posts) == 1

    # Verify the POST body is valid JSON with Discord-expected structure
    import json
    body = json.loads(posts[0].decode("utf-8"))
    assert "content" in body
    assert "agent-1" in body["content"]

    # Outbox drained, no errors
    assert mirror.outbox == []
    assert mirror.errors == []


async def test_discord_webhook_network_failure_does_not_break_session():
    """When the webhook is unreachable, the session continues unaffected.

    This is the key safety property: observation must never kill the session.
    """
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unreachable")

    mirror = make_mirror(handler)
    mirror.enqueue({"thread_id": "t1", "author": "a", "content": "msg"})

    # flush() must NOT raise even on network failure
    sent = await mirror.flush()
    assert sent == 0
    assert len(mirror.errors) == 1
    assert isinstance(mirror.errors[0], httpx.ConnectError)
