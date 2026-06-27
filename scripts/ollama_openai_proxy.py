"""
Ollama OpenAI-compat proxy for ST chat-completions mode.

Why this exists
===============
ST's text-completions mode drops chat history from the prompt (see
`scripts/notes/V2_DIRECTOR_BUG.md` for full trace). Switching to
chat-completions mode (main_api='openai' + chat_completion_source='custom')
preserves chat history, but Qwen3.6-35B-A3B on Ollama's `/v1/chat/completions`
endpoint ignores `think: false` and burns the full max_tokens budget on
internal thinking, leaving visible content empty.

This proxy accepts OpenAI-format requests from ST on port 11435, translates
them to Ollama's native `/api/chat` format with `think: false`, then
re-emits the response in OpenAI streaming (SSE) format so ST treats it as a
normal OpenAI-compatible endpoint.

Mapping
=======
OpenAI request body:
    {
        model: "fredrezones...",
        messages: [{role: "system", content: "..."}, {role: "user", content: "..."}, ...],
        temperature: 0.7,
        top_p: 0.9,
        max_tokens: 1024,
        stream: true,
        stop: ["..."],
        ...
    }

→ Ollama /api/chat request body:
    {
        model: "fredrezones...",
        messages: [...same...],
        stream: true,
        think: false,
        options: {
            temperature: ...,
            top_p: ...,
            num_predict: ... (max_tokens),
            stop: [...],
        },
    }

Ollama response (NDJSON, one chunk per line):
    {"model": "...", "message": {"role": "assistant", "content": "delta"}, "done": false}
    {"model": "...", "message": {...}, "done": true, ...}

→ OpenAI streaming response (SSE):
    data: {"id": "chatcmpl-...", "object": "chat.completion.chunk", "model": "...", "choices": [{"index": 0, "delta": {"content": "delta"}, "finish_reason": null}]}\n\n
    data: [DONE]\n\n
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

from aiohttp import web

UPSTREAM_OLLAMA = os.environ.get("UPSTREAM_OLLAMA", "http://127.0.0.1:11434")
LISTEN_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "11435"))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-5s ollama-proxy %(message)s')
log = logging.getLogger("ollama-proxy")

# Headers / paths we don't proxy
HOP_BY_HOP = {
    "host", "content-length", "connection", "transfer-encoding",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "upgrade",
}


def _new_chatcmpl_id() -> str:
    return f"chatcmpl-proxy-{uuid.uuid4().hex[:24]}"


def _to_ollama_body(openai_body: dict[str, Any]) -> dict[str, Any]:
    """Translate OpenAI /v1/chat/completions body → Ollama /api/chat body."""
    ollama_options: dict[str, Any] = {}
    if "temperature" in openai_body:
        ollama_options["temperature"] = openai_body["temperature"]
    if "top_p" in openai_body:
        ollama_options["top_p"] = openai_body["top_p"]
    if "max_tokens" in openai_body and openai_body["max_tokens"]:
        ollama_options["num_predict"] = openai_body["max_tokens"]
    if "stop" in openai_body and openai_body["stop"]:
        # OpenAI accepts str or list, Ollama accepts list
        stop = openai_body["stop"]
        if isinstance(stop, str):
            stop = [stop]
        ollama_options["stop"] = stop
    if "seed" in openai_body and openai_body["seed"] is not None and openai_body["seed"] != -1:
        ollama_options["seed"] = openai_body["seed"]
    if "frequency_penalty" in openai_body:
        ollama_options["frequency_penalty"] = openai_body["frequency_penalty"]
    if "presence_penalty" in openai_body:
        ollama_options["presence_penalty"] = openai_body["presence_penalty"]
    # Long context — qwen3.6 native ctx 262144, but stay reasonable
    ollama_options.setdefault("num_ctx", 16384)

    body: dict[str, Any] = {
        "model": openai_body.get("model", ""),
        "messages": openai_body.get("messages", []),
        "stream": bool(openai_body.get("stream", False)),
        "think": False,  # CRITICAL — kills Qwen3.6 thinking block
        "options": ollama_options,
    }
    return body


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """Translate POST /v1/chat/completions → POST /api/chat."""
    try:
        openai_body = await request.json()
    except Exception as e:
        return web.json_response({"error": {"message": f"bad request body: {e}"}}, status=400)

    stream = bool(openai_body.get("stream", False))
    model = openai_body.get("model", "(unknown)")
    msgs = openai_body.get("messages", [])
    msgs_n = len(msgs) if isinstance(msgs, list) else -1
    log.info(f"chat_completions model={model} messages={msgs_n} stream={stream} body_keys={list(openai_body.keys())}")
    if isinstance(msgs, list) and msgs:
        for i, m in enumerate(msgs[:5]):
            role = m.get('role', '?') if isinstance(m, dict) else type(m).__name__
            content = (m.get('content', '') if isinstance(m, dict) else '')
            log.info(f"  msg[{i}] role={role} content={content[:80]!r}")
    else:
        log.info(f"  msgs type={type(msgs).__name__} value={str(msgs)[:200]!r}")

    ollama_body = _to_ollama_body(openai_body)

    # Upstream — Ollama native
    import aiohttp
    async with aiohttp.ClientSession() as session:
        if stream:
            # SSE response — proxy stream-by-stream
            response = web.StreamResponse(
                status=200,
                reason="OK",
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
            await response.prepare(request)

            chatcmpl_id = _new_chatcmpl_id()
            created = int(time.time())

            upstream = await session.post(
                f"{UPSTREAM_OLLAMA}/api/chat",
                json=ollama_body,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=600),
            )

            if upstream.status != 200:
                err_text = await upstream.text()
                log.error(f"ollama upstream {upstream.status}: {err_text[:300]}")
                err_payload = {
                    "id": chatcmpl_id,
                    "object": "error",
                    "created": created,
                    "model": model,
                    "error": {"message": f"ollama HTTP {upstream.status}", "raw": err_text[:500]},
                }
                await response.write(f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n".encode())
                await response.write(b"data: [DONE]\n\n")
                await response.write_eof()
                return response

            try:
                first_chunk = True
                async for raw_line in upstream.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = evt.get("message") or {}
                    delta = msg.get("content") or ""
                    # Skip reasoning if it ever leaks
                    if msg.get("reasoning"):
                        if evt.get("done"):
                            break
                        continue
                    openai_chunk = {
                        "id": chatcmpl_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": delta} if (delta or first_chunk) else {},
                            "finish_reason": "stop" if evt.get("done") else None,
                        }],
                    }
                    if first_chunk and not delta:
                        # Force at least an empty delta so ST recognises the chunk
                        openai_chunk["choices"][0]["delta"]["role"] = "assistant"
                    await response.write(f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n".encode())
                    first_chunk = False
                    if evt.get("done"):
                        break
                # Final [DONE] marker
                await response.write(b"data: [DONE]\n\n")
                await response.write_eof()
            except (ConnectionResetError, asyncio_CancelledError):
                log.warning("client disconnected mid-stream")
            except Exception as e:
                log.error(f"stream error: {e}")
                try:
                    await response.write_eof()
                except Exception:
                    pass
            return response
        else:
            # Non-streaming — single JSON response
            upstream = await session.post(
                f"{UPSTREAM_OLLAMA}/api/chat",
                json=ollama_body,
                timeout=aiohttp.ClientTimeout(total=600),
            )
            upstream_text = await upstream.text()
            if upstream.status != 200:
                return web.json_response(
                    {"error": {"message": f"ollama HTTP {upstream.status}", "raw": upstream_text[:500]}},
                    status=upstream.status,
                )
            try:
                evt = json.loads(upstream_text)
            except json.JSONDecodeError:
                return web.json_response({"error": {"message": "bad upstream json", "raw": upstream_text[:500]}}, status=502)

            msg = evt.get("message") or {}
            content = msg.get("content") or ""
            openai_resp = {
                "id": _new_chatcmpl_id(),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": evt.get("prompt_eval_count", 0),
                    "completion_tokens": evt.get("eval_count", 0),
                    "total_tokens": (evt.get("prompt_eval_count", 0) or 0) + (evt.get("eval_count", 0) or 0),
                },
            }
            return web.json_response(openai_resp)


async def handle_models(request: web.Request) -> web.Response:
    """Stub /v1/models — return upstream ollama /api/tags as OpenAI list."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{UPSTREAM_OLLAMA}/api/tags", timeout=aiohttp.ClientTimeout(total=10)) as r:
                tags = await r.json()
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)

    data = []
    for m in (tags.get("models") or []):
        name = m.get("name", "")
        if name:
            data.append({"id": name, "object": "model", "created": 0, "owned_by": "ollama"})
    return web.json_response({"object": "list", "data": data})


# We use asyncio.CancelledError — define name to avoid Unbound when stripping
asyncio_CancelledError = __import__("asyncio").CancelledError


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    return app


if __name__ == "__main__":
    log.info(f"ollama OpenAI-compat proxy → {UPSTREAM_OLLAMA}, listening on {LISTEN_HOST}:{LISTEN_PORT}")
    web.run_app(make_app(), host=LISTEN_HOST, port=LISTEN_PORT, print=None)