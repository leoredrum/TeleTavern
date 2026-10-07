"""Shared Telegram helpers: streaming reply with live preview + safe long-message send."""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Callable

from . import telegram_splitter as TS

log = logging.getLogger("tavern.telegram")


async def stream_reply(message, agen: AsyncIterator[str], *, placeholder_text: str = "…",
                       edit_interval: float = 0.8,
                       postprocess: Callable[[str], str] | None = None,
                       raw_sink: list | None = None) -> str:
    """Stream tokens into a placeholder message, then send the final text safely split.

    Returns the final (post-processed) text that was sent. If `raw_sink` is given,
    the raw (pre-postprocess) accumulated text is appended to it — callers that
    need to parse machine tags the postprocess step strips (e.g. 〔HP-N〕) read it.
    """
    placeholder = await message.reply_text(placeholder_text)
    full = ""
    last_edit = 0.0
    loop = asyncio.get_event_loop()
    try:
        async for piece in agen:
            if not piece:
                continue
            full += piece
            now = loop.time()
            if now - last_edit >= edit_interval:
                shown = postprocess(full) if postprocess else full
                if len(shown) > TS.STREAM_PREVIEW_LIMIT:
                    shown = shown[:TS.STREAM_PREVIEW_LIMIT] + "\n…▌"
                try:
                    await placeholder.edit_text(shown)
                except Exception:  # noqa: BLE001  (identical text / rate limit)
                    pass
                last_edit = now
    except Exception as exc:  # noqa: BLE001
        log.warning("stream error: %s (partial=%d chars)", exc, len(full))
        if not full:
            try:
                await placeholder.edit_text(f"❌ 生成失败：{str(exc)[:200]}")
            except Exception:  # noqa: BLE001
                pass
            return ""
    if raw_sink is not None:
        raw_sink.append(full)
    final = (postprocess(full) if postprocess else full).strip() or "（无回复）"
    await TS.send_long_message(message, final, first_message=placeholder)
    return final


def chunk_text(text: str, limit: int = 3800) -> list[str]:
    """Plain chunking for non-streamed notices (help, status)."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit and cur:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out or [text]
