"""v0.1b — read-only Discord observation mirror (D3 unblocks here).

Pull-based: subscribers enqueue, ``flush()`` posts to the Discord webhook.
The core NEVER reads anything back from Discord (§3.3 — channels are views).
"""

import httpx
import pytest

from agent_augury.channel.discord_mirror import DiscordWebhookMirror, mirror_from_config


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


def test_format_line_contains_thread_author_content():
    line = DiscordWebhookMirror.format_line(
        {"thread_id": "t9", "author": "agent-2", "content": "hello"}
    )
    assert "agent-2" in line and "hello" in line
