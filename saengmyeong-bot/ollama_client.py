#!/usr/bin/env python3
"""
ollama_client.py — direct POST to V2 Ollama proxy (saengmyeong-bot L1 fallback).

When the ST Bridge path times out / returns 503, saengmyeong-bot can
still produce Chinese replies by talking DIRECTLY to the V2 Ollama
proxy on :11435 (which fronts Ollama :11434 / Qwen3.6-35B-A3B).

This module is small and synchronous because the bot already runs
in an async context; callers wrap it with `asyncio.to_thread`.
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from typing import Any


def ollama_chat_completions(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    timeout_s: float = 90.0,
    max_tokens: int = 1024,
    temperature: float = 0.85,
) -> dict[str, Any]:
    """Direct synchronous POST to an OpenAI-compatible /v1/chat/completions
    endpoint. Returns the parsed JSON dict. Raises `urllib.error.HTTPError`
    on non-2xx.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "saengmyeong-bot/1.0 (L1-fallback)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))
