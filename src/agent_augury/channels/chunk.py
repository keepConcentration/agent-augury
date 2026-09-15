"""Chat message chunking (Hermes-style).

Split long outbound text at natural boundaries, preserve fenced code blocks,
and append ``(i/n)`` indicators on multi-chunk sends.

Reference: Hermes ``BasePlatformAdapter.truncate_message``
(``gateway/platforms/base.py``).
"""

from __future__ import annotations

# Room for " (XX/XX)" on multi-chunk messages.
_INDICATOR_RESERVE = 10
_FENCE_CLOSE = "\n```"


def split_chat_content(
    content: str,
    *,
    limit: int = 1800,
    indicators: bool = True,
) -> list[str]:
    """Split *content* into chunks of at most *limit* characters.

    Prefers newline, then space. When a split falls inside a `` ``` `` fence,
    closes the fence on the current chunk and reopens it on the next.
    """
    text = content or ""
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    carry_lang: str | None = None

    while remaining:
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""
        headroom = limit - _INDICATOR_RESERVE - len(prefix) - len(_FENCE_CLOSE)
        if headroom < 1:
            headroom = max(1, limit // 2)

        if len(prefix) + len(remaining) <= limit - _INDICATOR_RESERVE:
            final_chunk = prefix + remaining
            if carry_lang is not None:
                in_code = True
                for line in remaining.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("```"):
                        in_code = not in_code
                if in_code:
                    final_chunk += _FENCE_CLOSE
            chunks.append(final_chunk)
            break

        region = remaining[:headroom]
        split_at = region.rfind("\n")
        if split_at < headroom // 2:
            split_at = region.rfind(" ")
        if split_at < 1:
            split_at = max(1, headroom)

        # Avoid splitting inside an inline `code` span (odd backtick count).
        candidate = remaining[:split_at]
        backtick_count = candidate.count("`") - candidate.count("\\`")
        if backtick_count % 2 == 1:
            last_bt = candidate.rfind("`")
            while last_bt > 0 and candidate[last_bt - 1] == "\\":
                last_bt = candidate.rfind("`", 0, last_bt)
            if last_bt > 0:
                safe = max(
                    candidate.rfind(" ", 0, last_bt),
                    candidate.rfind("\n", 0, last_bt),
                )
                if safe > headroom // 4:
                    split_at = safe

        chunk_body = remaining[:split_at]
        remaining = remaining[split_at:].lstrip()
        full_chunk = prefix + chunk_body

        in_code = carry_lang is not None
        lang = carry_lang or ""
        for line in chunk_body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_code:
                    in_code = False
                    lang = ""
                else:
                    in_code = True
                    tag = stripped[3:].strip()
                    lang = tag.split()[0] if tag else ""

        if in_code:
            full_chunk += _FENCE_CLOSE
            carry_lang = lang
        else:
            carry_lang = None

        chunks.append(full_chunk)

    if indicators and len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{chunk} ({i + 1}/{total})" for i, chunk in enumerate(chunks)]

    return chunks
