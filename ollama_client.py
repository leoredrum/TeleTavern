"""Ollama client — async streaming chat for Qwen3 MoE / uncensored models.

Why /api/chat, not /v1/chat/completions
=======================================
Ollama 0.30 + Qwen3.6-A3B ignores top-level `think: false` and emits the entire
output into the `reasoning` field of the /v1/chat/completions delta. The model
burns the full max_tokens budget on internal thinking and the user-visible
content stays empty. The same model on the native /api/chat endpoint honours
`think: false` and produces visible content normally — same as ollama's own
benchmark scripts.

Streaming format on /api/chat is one JSON object per line:
  {"model": "...", "created_at": "...", "message": {"role":"assistant",
   "content":"..."}, "done": false}
  ...
  {"message": {...}, "done": true, "total_duration": ..., ...}

We still drop any `reasoning` field defensively in case a future ollama
version re-introduces it.
"""
from __future__ import annotations

import json
import re
from typing import AsyncIterator

import aiohttp


class OllamaChatError(RuntimeError):
    pass


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.95,
        top_p: float = 0.92,
        repeat_penalty: float = 1.1,
        repeat_last_n: int = 512,
        presence_penalty: float = 0.4,
        frequency_penalty: float = 0.25,
        top_k: int = 40,
        min_p: float = 0.05,
    ) -> None:
        # base_url is like http://localhost:11434
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        # Anti-loop sampling (SillyTavern-style defaults). The old setup had
        # repeat_penalty=1.18 but left repeat_last_n at Ollama's default 64 tokens
        # (< 1.5 Chinese sentences), so cross-turn repetition was never penalised.
        # A wide window + presence penalty catches it; repeat_penalty is lowered
        # so the wider window doesn't start mangling ordinary vocabulary.
        self.repeat_penalty = repeat_penalty
        self.repeat_last_n = repeat_last_n
        self.presence_penalty = presence_penalty
        self.frequency_penalty = frequency_penalty
        self.top_k = top_k
        self.min_p = min_p

    async def stream_chat(
        self,
        messages: list[dict],
        *,
        cancel_event=None,
    ) -> AsyncIterator[str]:
        """Yield visible reply tokens (thinking stripped)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            # qwen3 thinks by default; that burns our entire max_tokens budget on
            # reasoning text and leaves content empty. Disable it so the model
            # produces its reply directly. The /api/chat endpoint honours this
            # for ollama >= 0.5 (the /v1/chat/completions endpoint does NOT on
            # qwen3.6-A3B as of ollama 0.30.10 — verified 2026-06-25).
            "think": False,
            "options": {
                # Long context — qwen3:32b supports 40k natively. Set high enough
                # so character card + conversation history fits.
                "num_ctx": 32768,
                "num_predict": self.max_tokens,      # was never sent → unbounded replies
                "temperature": self.temperature,
                "top_p": self.top_p,
                "top_k": self.top_k,
                "min_p": self.min_p,
                "repeat_penalty": self.repeat_penalty,
                "repeat_last_n": self.repeat_last_n,
                "presence_penalty": self.presence_penalty,
                "frequency_penalty": self.frequency_penalty,
                "think": False,
            },
        }
        # Native ollama endpoint — /api/chat honours think:false for qwen3.x
        # while /v1/chat/completions does not.
        url = f"{self.base_url}/api/chat"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise OllamaChatError(f"ollama HTTP {resp.status}: {body[:500]}")
                    in_think = False
                    async for raw_line in resp.content:
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = evt.get("message") or {}
                        # Drop any reasoning chunk defensively.
                        if msg.get("reasoning"):
                            continue
                        chunk = msg.get("content") or ""
                        if not chunk:
                            if evt.get("done"):
                                break
                            continue
                        # Strip <think>...</think> blocks inline in case ollama puts
                        # them in content. Track open/close state across chunks.
                        for piece in _split_think(chunk, in_think):
                            text, in_think = piece
                            if text:
                                yield text
                        if evt.get("done"):
                            break
        except aiohttp.ClientError as e:
            raise OllamaChatError(f"ollama connection error: {e}") from e


def _split_think(chunk: str, in_think: bool) -> list[tuple[str, bool]]:
    """Split a delta string around <think>...</think>, yielding visible parts.

    Returns a list of (text, new_in_think_state) tuples.
    """
    THINK_OPEN = "<think>"
    THINK_CLOSE = "</think>"
    out: list[tuple[str, bool]] = []
    s = chunk
    while s:
        if not in_think:
            i = s.find(THINK_OPEN)
            if i == -1:
                out.append((s, False))
                return out
            if i > 0:
                out.append((s[:i], False))
            s = s[i + len(THINK_OPEN):]
            in_think = True
        else:
            j = s.find(THINK_CLOSE)
            if j == -1:
                # drop everything until we see the close tag
                return out
            s = s[j + len(THINK_CLOSE):]
            in_think = False
    if not out:
        out.append(("", in_think))
    return out
