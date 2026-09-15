"""Web tools: WebSearchProvider abstraction + SSRF-safe fetch helpers.

DESIGN ref: docs/AGENT_TOOLS_EXPANSION_DESIGN.md §4.3 (v4.1 확정).

- ``fetch_url_safe()`` — SSRF-safe URL fetch. Blocks private/link-local/CGNAT
  IP literals, cloud metadata hostnames, integer/hex IP obfuscations, and
  re-validates every redirect hop (agent-1 feedback #1/#2/#6 + Hermes
  url_safety.py benchmark).
- ``WebSearchProvider`` — search-backend abstraction (DuckDuckGo default,
  Serper/Tavily behind API keys). Returns metadata only (title/url/snippet);
  full pages go through ``fetch_url_safe``.

This module is deliberately independent of ``ToolPolicy`` so it can be
unit-tested in isolation; the policy object in tools.py passes its bounds in.
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import os
import re
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

# ---------------------------------------------------------------------------
# SSRF helpers
# ---------------------------------------------------------------------------

# Cloud metadata hostnames that must ALWAYS be blocked, regardless of the
# private-IP toggle (Hermes url_safety.py benchmark).
_ALWAYS_BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.azure.internal",
        "metadata.goog",
        "169.254.169.254",  # AWS/GCP/Azure/DO/Oracle metadata
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba Cloud metadata
    }
)

# CGNAT / Shared Address Space (RFC 6598) — ipaddress.is_private does NOT
# cover 100.64.0.0/10, so it must be blocked explicitly.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

# Integer / hex IP literal forms that ``ipaddress.ip_address`` rejects but
# that OS resolvers (inet_aton) interpret as dotted IPv4 — e.g. 2130706433
# == 127.0.0.1, 0x7f000001 == 127.0.0.1. These are never legitimate public
# hostnames (all-digits / 0x-prefixed), so block them outright (v0.7-2).
_INTEGER_IP_LITERAL = re.compile(r"^(?:0[xX][0-9a-fA-F]+|\d+)$")


def _is_cgnat(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.version == 4 and ip in _CGNAT_NETWORK


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def is_blocked_host(
    host: str,
    *,
    deny_domains: tuple[str, ...] = (),
    block_private_ips: bool = True,
    always_blocked_hostnames: frozenset[str] = _ALWAYS_BLOCKED_HOSTNAMES,
) -> bool:
    """SSRF block decision (P10 exact-suffix + private/link-local/CGNAT/
    IPv4-mapped-IPv6 + cloud metadata + integer/hex IP obfuscation).

    Returns True (= block) for:
      - cloud metadata hostnames / IPs (always, even when toggle off)
      - hosts matching ``deny_domains`` (exact or ``.``-suffix — P10)
      - IP literals in private/loopback/link-local/multicast/reserved/
        unspecified/CGNAT ranges (when ``block_private_ips``)
      - integer / hex IP obfuscations (``2130706433``, ``0x7f000001``)

    Domain names that are not IP literals are NOT DNS-resolved here
    (DNS re-resolution + connect-time validation is v1.1).
    """
    host = _normalize_host(host)
    if not host:
        return True

    # 1) Always-blocked metadata hostnames (exact or subdomain).
    if host in always_blocked_hostnames or any(
        host.endswith("." + d) for d in always_blocked_hostnames
    ):
        return True

    # 2) deny_domains — exact or exact-suffix (P10; no `notexample.com` false match).
    if host in deny_domains or any(host.endswith("." + d) for d in deny_domains):
        return True

    # 3) IP literal → ipaddress range check (v0.7-2, not deferred to v1.1).
    if block_private_ips:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # Integer/hex IP literal forms that ipaddress rejects but OS
            # resolvers interpret as private IPv4 (2130706433 → 127.0.0.1).
            # Not a legitimate public hostname — block outright.
            if _INTEGER_IP_LITERAL.match(host):
                return True
        else:
            # IPv4-mapped IPv6 (::ffff:x.x.x.x) → unwrap before checks.
            if ip.version == 6 and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
                or _is_cgnat(ip)
            ):
                return True
    return False


def _html_to_text(raw: str) -> str:
    """Strip HTML to plain text using stdlib only (no extra dependency)."""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Search provider abstraction (agent-3 proposal + Hermes web_tools benchmark)
# ---------------------------------------------------------------------------


class WebSearchProvider(ABC):
    """Search backend abstraction — DuckDuckGo (default, keyless) / Serper /
    Tavily / (SearXNG in v0.9)."""

    name: str = "base"

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        """Return ``[{"title", "url", "snippet"}, ...]`` (at most max_results)."""

    async def aclose(self) -> None:
        """Release any owned HTTP client (default no-op)."""


def _extract_ddg_url(href: str) -> str:
    """Resolve a DuckDuckGo result URL to its real target.

    DDG wraps results in ``//duckduckgo.com/l/?uddg=<encoded-url>``; the
    encoded ``uddg`` parameter holds the actual destination.
    """
    parsed = urlparse(href)
    if parsed.netloc == "duckduckgo.com" and parsed.path == "/l/":
        qs = parse_qs(parsed.query)
        if qs.get("uddg"):
            return qs["uddg"][0]
    return href


class DuckDuckGoProvider(WebSearchProvider):
    """DuckDuckGo HTML endpoint — no API key required (model-agnostic philosophy)."""

    name = "duckduckgo"
    _URL = "https://html.duckduckgo.com/html/"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        try:
            resp = await self._client.post(
                self._URL,
                data={"q": query},
                headers={"User-Agent": "agent-augury/0.7"},
            )
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            return [{"error": f"web_search failed: {exc}"}]

        results: list[dict[str, Any]] = []
        # Match result links and their snippets.
        for m in re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            resp.text,
            re.DOTALL,
        ):
            href, title_html = m.group(1), m.group(2)
            title = _html_to_text(title_html)
            url = _extract_ddg_url(href)
            # Snippet: first result__snippet anchor after this result link.
            tail = resp.text[m.end():]
            snippet_m = re.search(
                r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
                tail,
                re.DOTALL,
            )
            snippet = _html_to_text(snippet_m.group(1)) if snippet_m else ""
            results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= max_results:
                break
        return results

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class _ApiKeyProvider(WebSearchProvider):
    """Base for API-key providers (Serper/Tavily) — key from env, never stored in config."""

    api_key_env: str = ""
    _url: str = ""
    _key_header: str = ""

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key or os.environ.get(self.api_key_env, "")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def _post_json(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        headers = {"Content-Type": "application/json"}
        if self._key_header:
            headers[self._key_header] = self._api_key
        else:
            payload = {**payload, "api_key": self._api_key}
        try:
            resp = await self._client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            return {"_error": f"web_search failed: {exc}"}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class SerperProvider(_ApiKeyProvider):
    """Serper.dev — requires ``SERPER_API_KEY`` env var."""

    name = "serper"
    api_key_env = "SERPER_API_KEY"
    _url = "https://google.serper.dev/search"
    _key_header = "X-API-KEY"

    async def search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        data = await self._post_json({"q": query, "num": max_results})
        if not data or "_error" in data:
            return [{"error": data.get("_error", "web_search failed")}]
        results = []
        for item in (data.get("organic") or [])[:max_results]:
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                }
            )
        return results


class TavilyProvider(_ApiKeyProvider):
    """Tavily — requires ``TAVILY_API_KEY`` env var."""

    name = "tavily"
    api_key_env = "TAVILY_API_KEY"
    _url = "https://api.tavily.com/search"
    _key_header = ""

    async def search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        data = await self._post_json({"query": query, "max_results": max_results})
        if not data or "_error" in data:
            return [{"error": data.get("_error", "web_search failed")}]
        results = []
        for item in (data.get("results") or [])[:max_results]:
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", ""),
                }
            )
        return results


def build_search_provider(
    provider: str,
    *,
    timeout: float = 15.0,
    client: httpx.AsyncClient | None = None,
) -> WebSearchProvider | None:
    """Resolve ``tools.web.search_provider`` → provider instance.

    Returns None when the requested provider needs an API key that is not
    set (caller falls back to DuckDuckGo or reports a config error).
    """
    provider = (provider or "duckduckgo").lower().strip()
    if provider == "duckduckgo":
        return DuckDuckGoProvider(client=client, timeout=timeout)
    if provider == "serper":
        if not os.environ.get("SERPER_API_KEY"):
            return None
        return SerperProvider(client=client, timeout=timeout)
    if provider == "tavily":
        if not os.environ.get("TAVILY_API_KEY"):
            return None
        return TavilyProvider(client=client, timeout=timeout)
    return None  # searxng 등 v0.9에서 추가


# ---------------------------------------------------------------------------
# SSRF-safe fetch (fetch_url implementation, §4.3.1)
# ---------------------------------------------------------------------------


async def fetch_url_safe(
    url: str,
    *,
    deny_domains: tuple[str, ...] = (),
    block_private_ips: bool = True,
    timeout: float = 15.0,
    max_bytes: int = 65_536,
    max_redirects: int = 5,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch a URL with SSRF protection and per-hop redirect re-validation.

    Follows redirects manually (``follow_redirects=False``) and re-checks
    every hop's host against the same policy — an external URL that 302s to
    ``http://169.254.169.254/`` is blocked (agent-1 feedback #1).

    Returns a JSON-serializable dict:
      ``{"status_code", "url", "content_type", "content", "truncated"}``
    or ``{"error": ...}`` on policy violation / network failure.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"error": "only http/https URLs are allowed"}
    if is_blocked_host(
        parsed.hostname or "",
        deny_domains=deny_domains,
        block_private_ips=block_private_ips,
    ):
        return {"error": f"host blocked by SSRF policy: {parsed.hostname}"}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    current_url = url
    try:
        resp = None
        for _hop in range(max_redirects):
            resp = await client.get(
                current_url, headers={"User-Agent": "agent-augury/0.7"}
            )
            if resp.is_redirect and "location" in resp.headers:
                next_url = urljoin(current_url, resp.headers["location"])
                next_host = urlparse(next_url).hostname or ""
                if is_blocked_host(
                    next_host,
                    deny_domains=deny_domains,
                    block_private_ips=block_private_ips,
                ):
                    return {"error": f"redirect target blocked by SSRF policy: {next_host}"}
                current_url = next_url
                continue
            break

        text = _html_to_text(resp.text) if resp is not None else ""
        return {
            "status_code": resp.status_code if resp is not None else 0,
            "url": str(resp.url) if resp is not None else current_url,
            "content_type": resp.headers.get("content-type", "") if resp is not None else "",
            "content": text[:max_bytes],
            "truncated": len(text) > max_bytes,
        }
    except httpx.TimeoutException as exc:
        return {"error": f"fetch timed out: {exc}"}
    except httpx.RequestError as exc:
        return {"error": f"fetch failed: {exc}"}
    finally:
        if owns_client:
            await client.aclose()
