"""Tests for agent/web.py — SSRF-safe fetch + web search providers.

DESIGN ref: AGENT_TOOLS_EXPANSION_DESIGN.md §4.3 (v4.1 확정), v0.7-2 통과 기준.
"""

from __future__ import annotations

import httpx

from agent_augury.agent.web import (
    DuckDuckGoProvider,
    build_search_provider,
    fetch_url_safe,
    is_blocked_host,
)

# ---------------------------------------------------------------------------
# is_blocked_host — SSRF policy (v0.7-2 통과 기준)
# ---------------------------------------------------------------------------


def test_blocked_host_private_ip_literals():
    for host in ("10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1"):
        assert is_blocked_host(host), f"{host} should be blocked"


def test_blocked_host_metadata_and_linklocal():
    for host in (
        "169.254.169.254",  # cloud metadata
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba metadata
        "metadata.google.internal",
        "metadata.azure.internal",
    ):
        assert is_blocked_host(host), f"{host} should be blocked"


def test_blocked_host_integer_ip_obfuscation():
    """Decimal/hex integer IPs must be blocked (2130706433 == 127.0.0.1)."""
    assert is_blocked_host("2130706433")


def test_blocked_host_cgnat():
    """CGNAT 100.64.0.0/10 — is_private misses it, must be explicit."""
    assert is_blocked_host("100.64.0.1")
    assert is_blocked_host("100.127.255.254")


def test_blocked_host_ipv4_mapped_ipv6():
    """IPv4-mapped IPv6 (::ffff:x.x.x.x) unwraps to the embedded IPv4."""
    assert is_blocked_host("::ffff:127.0.0.1")
    assert is_blocked_host("::ffff:169.254.169.254")


def test_blocked_host_deny_domains_exact_and_suffix():
    """P10: exact match + '.domain' suffix; NOT 'notexample.com'."""
    deny = ("example.com",)
    assert is_blocked_host("example.com", deny_domains=deny)
    assert is_blocked_host("sub.example.com", deny_domains=deny)
    assert not is_blocked_host("notexample.com", deny_domains=deny)
    assert not is_blocked_host("example.com.evil.com", deny_domains=deny)


def test_blocked_host_public_domain_not_blocked():
    assert not is_blocked_host("example.com")
    assert not is_blocked_host("docs.python.org")


def test_blocked_host_toggle_off_still_blocks_metadata():
    """Cloud metadata is always blocked, even when private-IP blocking is off."""
    assert is_blocked_host("169.254.169.254", block_private_ips=False)
    assert is_blocked_host("metadata.google.internal", block_private_ips=False)
    # But plain private IP is allowed when toggle off.
    assert not is_blocked_host("192.168.1.1", block_private_ips=False)


# ---------------------------------------------------------------------------
# fetch_url_safe — SSRF + redirect revalidation + truncation
# ---------------------------------------------------------------------------


async def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_url_blocks_private_ip_literal():
    client = await _make_client(lambda request: httpx.Response(200, text="ok"))
    result = await fetch_url_safe(
        "http://169.254.169.254/latest/meta-data/", client=client
    )
    assert "error" in result
    assert "blocked" in result["error"]


async def test_fetch_url_blocks_integer_ip():
    client = await _make_client(lambda request: httpx.Response(200, text="ok"))
    result = await fetch_url_safe("http://2130706433/", client=client)
    assert "error" in result


async def test_fetch_url_blocks_cgnat():
    client = await _make_client(lambda request: httpx.Response(200, text="ok"))
    result = await fetch_url_safe("http://100.64.0.1/", client=client)
    assert "error" in result


async def test_fetch_url_blocks_ipv4_mapped_ipv6():
    client = await _make_client(lambda request: httpx.Response(200, text="ok"))
    result = await fetch_url_safe("http://[::ffff:127.0.0.1]/", client=client)
    assert "error" in result


async def test_fetch_url_redirect_to_metadata_blocked():
    """external URL → 302 → 169.254.169.254 must be blocked (agent-1 #1)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example.com":
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        return httpx.Response(200, text="internal")

    client = await _make_client(handler)
    result = await fetch_url_safe("http://public.example.com/", client=client)
    assert "error" in result
    assert "redirect target blocked" in result["error"]


async def test_fetch_url_ok_and_truncated():
    body = "<html><body><h1>Hello</h1><p>" + "x" * 1000 + "</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    client = await _make_client(handler)
    result = await fetch_url_safe(
        "https://example.com/", client=client, max_bytes=100
    )
    assert result["status_code"] == 200
    assert "Hello" in result["content"]
    assert result["truncated"] is True
    assert len(result["content"]) <= 100


async def test_fetch_url_deny_domain():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    client = await _make_client(handler)
    result = await fetch_url_safe(
        "https://example.com/", deny_domains=("example.com",), client=client
    )
    assert "error" in result
    # subdomain allowed when only exact domain denied? No — suffix applies.
    result2 = await fetch_url_safe(
        "https://sub.example.com/", deny_domains=("example.com",), client=client
    )
    assert "error" in result2
    # unrelated domain fine
    result3 = await fetch_url_safe(
        "https://other.com/", deny_domains=("example.com",), client=client
    )
    assert result3["status_code"] == 200


async def test_fetch_url_scheme_rejected():
    client = await _make_client(lambda request: httpx.Response(200, text=""))
    result = await fetch_url_safe("file:///etc/passwd", client=client)
    assert "error" in result
    assert "only http/https" in result["error"]


# ---------------------------------------------------------------------------
# Web search providers
# ---------------------------------------------------------------------------


async def test_duckduckgo_provider_parses_results():
    html = """
    <html><body>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">Example <b>Title</b></a>
    <a class="result__snippet" href="https://example.com/">The snippet text here.</a>
    <a class="result__a" href="https://other.com/">Other Result</a>
    </body></html>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, text=html)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DuckDuckGoProvider(client=client)
    results = await provider.search("python", max_results=5)
    assert len(results) >= 2
    first = results[0]
    assert first["title"] == "Example Title"
    assert first["url"] == "https://example.com/page"  # uddg decoded
    assert "snippet text" in first["snippet"]
    await provider.aclose()


async def test_build_search_provider_resolution():
    ddg = build_search_provider("duckduckgo")
    assert ddg is not None
    assert ddg.name == "duckduckgo"
    await ddg.aclose()

    # Missing API key → None (caller falls back to DDG or errors)
    import os

    for key in ("SERPER_API_KEY", "TAVILY_API_KEY"):
        old = os.environ.pop(key, None)
        try:
            assert build_search_provider("serper") is None
            assert build_search_provider("tavily") is None
        finally:
            if old is not None:
                os.environ[key] = old

    # Unknown provider → None
    assert build_search_provider("nope") is None
