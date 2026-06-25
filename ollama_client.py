"""Async ollama client with streaming chat completions.

Uses the OpenAI-compatible /v1/chat/completions endpoint with stream=true so we
can show tokens to the user as they arrive. Filters out <think>...</think>
reasoning blocks from qwen3 models so the visible reply is just the roleplay
output.
"""
from __future__ import annotations

import json
import re
from typing import AsyncIterator

import aiohttp


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# Some qwen3 variants emit partial think blocks mid-stream; strip any orphan
# opening tag the model produced without closing it.
_OPEN_THINK_RE = re.compile(r"<think>.*", re.DOTALL)


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
        repeat_penalty: float = 1.18,
    ) -> None:
        # base_url is like http://localhost:11434 — we'll use the /v1 prefix.
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.repeat_penalty = repeat_penalty

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
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "repeat_penalty": self.repeat_penalty,
            # qwen3 thinks by default; that burns our entire max_tokens budget on
            # reasoning text and leaves content empty. Disable it so the model
            # produces its reply directly. ollama >= 0.5 honours top-level `think`.
            "think": False,
            "options": {
                # Long context — qwen3:32b supports 40k natively. Set high enough
                # so character card + conversation history fits.
                "num_ctx": 32768,
                # Match top-level sampling params for ollama-native path.
                "temperature": self.temperature,
                "top_p": self.top_p,
                "repeat_penalty": self.repeat_penalty,
                # Disable thinking.
                "think": False,
            },
        }
        url = f"{self.base_url}/v1/chat/completions"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise OllamaChatError(f"ollama HTTP {resp.status}: {body[:500]}")
                    buf_visible = ""
                    buf_thinking = ""
                    in_think = False
                    async for raw_line in resp.content:
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if data == "[DONE]":
                            break
                        try:
                            evt = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = evt.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        # OpenAI-compatible chat completions don't separate reasoning.
                        # With ollama + qwen3 the reasoning often arrives in a separate
                        # 'reasoning' field on the delta (ollama >= 0.5). When that's
                        # present, drop it.
                        if "reasoning" in delta and delta["reasoning"]:
                            buf_thinking += delta["reasoning"]
                            continue
                        chunk = delta.get("content") or ""
                        if not chunk:
                            continue
                        # Strip <think>...</think> blocks inline in case ollama puts them
                        # in content. Track open/close state across chunks.
                        for piece in _split_think(chunk, in_think):
                            text, in_think = piece
                            if text:
                                buf_visible += text
                                yield text
                    # Flush any remaining thinking buffer (do not yield).
                    _ = buf_thinking
                    _ = buf_visible
        except aiohttp.ClientError as e:
            raise OllamaChatError(f"ollama connection error: {e}") from e


def _split_think(chunk: str, in_think: bool) -> list[tuple[str, bool]]:
    """Split a delta string around <think>...</think>, yielding visible parts.

    Returns a list of (text, new_in_think_state) tuples.
    """
    out: list[tuple[str, bool]] = []
    s = chunk
    while s:
        if not in_think:
            # Look for opening <think>
            i = s.find("<think>")
            if i == -1:
                out.append((s, False))
                return out
            if i > 0:
                out.append((s[:i], False))
            s = s[i + len("<think>") :]
            in_think = True
        else:
            j = s.find("</think>")
            if j == -1:
                # drop everything until we see the close tag
                return out
            s = s[j + len("</think>") :]
            in_think = False
    if not out:
        out.append(("", in_think))
    return out