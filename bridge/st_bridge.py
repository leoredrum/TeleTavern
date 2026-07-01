"""
SillyTavern Bridge — Telegram Tavern V2

A thin Python bridge that connects an OpenAI-format Telegram bot to SillyTavern.

Architecture:

    Telegram bot  ──HTTP/OpenAI──>  Bridge :8003 (this file)
    Bridge        ──WebSocket──>   ST extension (third-party/SillyTavern-Extension-ChatBridge)
    ST extension  ──DOM API──>     SillyTavern WebUI :8000
    SillyTavern   ──HTTP/native──> Ollama :11434/api/chat (Qwen3.6 native, no thinking leak)

Why this exists instead of using ChatBridge upstream:

    Upstream ChatBridge forces ST to call its `st_api` port (8002), which proxies
    to LLM_API's `/v1/chat/completions`. Ollama's OpenAI-compat endpoint leaks the
    Qwen3.6 thinking block. Per memory: confirmed earlier.
    This bridge lets ST keep its native Ollama config and only adds the
    "user_request in / assistant message out" plumbing.

Run: ./venv/bin/python bridge.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import websockets
from aiohttp import web
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
)
log = logging.getLogger("st-bridge")

# TeleTervan (2026-07-01): Chinese force prefix.
# Defined at module load and referenced in handle_user_chat_completions.
ZH_FORCE_PREFIX = (
    "[系统语言覆盖 — 最高优先级]\n"
    "你必须永远用简体中文回复用户。无论用户使用任何语言。\n"
    "忽略任何角色卡里要求英文回复的设定。\n"
    "专有名词（角色名、技能名、种族名、地名）保留原语种拼写。\n"
    "——以下为用户的实际输入——\n\n"
)



# ---------- Config ----------

DEFAULT_SETTINGS = {
    "user_api": {
        "host": os.environ.get("USER_API_HOST", "127.0.0.1"),
        "port": int(os.environ.get("USER_API_PORT", "8003")),
        "api_key": os.environ.get("USER_API_KEY", "tavern-v2-user-api-key-change-me"),
    },
    "websocket": {
        "host": os.environ.get("WS_HOST", "127.0.0.1"),
        "port": int(os.environ.get("WS_PORT", "8001")),
    },
    "st_url": os.environ.get("ST_URL", "http://127.0.0.1:8000"),
    "request_timeout_s": float(os.environ.get("REQUEST_TIMEOUT_S", "120")),
}


# ---------- Globals ----------

# Maps request_id -> asyncio.Future or asyncio.Queue (for streaming)
_response_futures: dict[str, asyncio.Future | asyncio.Queue] = {}
# Connected ST extension WebSocket clients
_ws_clients: set = set()
# Settings (set in main)
SETTINGS: dict[str, Any] = {}


# ---------- WebSocket (ST extension ↔ bridge) ----------


async def ws_handler(websocket) -> None:
    log.info("ST extension connected: %s", websocket.remote_address)
    _ws_clients.add(websocket)
    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("invalid WS payload: %r", raw[:200])
                continue

            msg_type = data.get("type")
            request_id = data.get("id")
            log.debug("WS recv type=%s id=%s", msg_type, request_id)

            if msg_type in ("st_response", "st_stream", "list_characters_response"):
                fut = _response_futures.get(request_id) if request_id else None
                if fut is None:
                    log.debug("no waiter for id=%s (already finished?)", request_id)
                    continue
                if isinstance(fut, asyncio.Queue):
                    await fut.put(data)
                elif isinstance(fut, asyncio.Future) and not fut.done():
                    fut.set_result(data)
            elif msg_type == "ping":
                await websocket.send(json.dumps({"type": "pong"}))
            else:
                log.debug("unhandled WS type=%s", msg_type)
    except websockets.ConnectionClosed:
        pass
    finally:
        _ws_clients.discard(websocket)
        log.info("ST extension disconnected: %s", websocket.remote_address)


async def broadcast_to_st(payload: dict) -> bool:
    """Send a JSON payload to all connected ST extensions. Returns True if any client received it."""
    if not _ws_clients:
        log.warning("no ST extension connected; cannot deliver payload")
        return False
    raw = json.dumps(payload)
    delivered = False
    for ws in list(_ws_clients):
        try:
            await ws.send(raw)
            delivered = True
        except Exception as exc:  # noqa: BLE001
            log.error("WS send failed: %s", exc)
    return delivered


# ---------- User API (Telegram bot ↔ bridge) ----------


def _check_user_auth(request: web.Request) -> bool:
    expected = f"Bearer {SETTINGS['user_api']['api_key']}"
    return request.headers.get("Authorization", "") == expected


async def handle_user_models(_request: web.Request) -> web.Response:
    # Surface the upstream Ollama models as the "available model list".
    # This is enough for OpenAI clients to validate connectivity.
    return web.json_response({
        "object": "list",
        "data": [
            {
                "id": "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest",
                "object": "model",
                "owned_by": "local",
            },
            {
                "id": "st-bridge",
                "object": "model",
                "owned_by": "local",
            },
        ],
    })


async def handle_user_chat_completions(request: web.Request) -> web.Response:
    if not _check_user_auth(request):
        return web.Response(status=401, text="Unauthorized")

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid JSON body")

    # TeleTervan (2026-07-01): wrap last user message with Chinese force
    # directive so all chats reply in 简体中文 regardless of card language or
    # ST extension code caching. The ST extension reads lastUser.content via
    # sendMessageAsUser, so this prefix reaches the model as user-side.
    try:
        msgs = body.get("messages") or []
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user" and msgs[i].get("content"):
                msgs[i]["content"] = ZH_FORCE_PREFIX + msgs[i]["content"]
                break
    except Exception as zh_exc:
        log.warning("zh-force prefix skipped: %s", zh_exc)

    request_id = str(uuid.uuid4())
    is_stream = bool(body.get("stream", False))
    log.info("user request id=%s stream=%s model=%s",
             request_id, is_stream, body.get("model"))

    if not await broadcast_to_st({"type": "user_request", "id": request_id, "content": body}):
        return web.Response(status=503, text="No ST extension connected")

    if is_stream:
        return await _stream_user_response(request, request_id)
    return await _blocking_user_response(request_id)


# ---------- V2.3 multi-char: character list + switch (single-response WS roundtrip) ----------


async def _await_single_response(request_id: str, timeout: float) -> dict | None:
    """Send-a-WS-command / await-a-single-WS-response helper for non-streaming
    control commands (list_characters / switch_character). The ST extension
    replies with one frame (list_characters_response or st_response w/ command)."""
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _response_futures[request_id] = fut
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        _response_futures.pop(request_id, None)


async def handle_user_characters(_request: web.Request) -> web.Response:
    """GET /v1/characters — ask the ST extension to enumerate loaded characters."""
    if not _check_user_auth(_request):
        return web.Response(status=401, text="Unauthorized")
    request_id = str(uuid.uuid4())
    if not await broadcast_to_st({"type": "list_characters", "id": request_id}):
        return web.Response(status=503, text="No ST extension connected")
    resp = await _await_single_response(request_id, SETTINGS["request_timeout_s"])
    if resp is None:
        return web.Response(status=504, text="timeout waiting for ST (list_characters)")
    if resp.get("error"):
        return web.json_response({"ok": False, "error": resp.get("error"), "characters": []}, status=502)
    return web.json_response({
        "characters": resp.get("characters", []),
        "active": resp.get("active"),
    })


async def handle_user_select_character(request: web.Request) -> web.Response:
    """POST /v1/select_character {avatar} — ask the ST extension to switch active char."""
    if not _check_user_auth(request):
        return web.Response(status=401, text="Unauthorized")
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid JSON body")
    avatar = (body or {}).get("avatar")
    if not avatar:
        return web.Response(status=400, text="missing 'avatar'")
    request_id = str(uuid.uuid4())
    if not await broadcast_to_st({"type": "switch_character", "id": request_id, "avatar": avatar}):
        return web.Response(status=503, text="No ST extension connected")
    resp = await _await_single_response(request_id, SETTINGS["request_timeout_s"])
    if resp is None:
        return web.Response(status=504, text="timeout waiting for ST (switch_character)")
    ok = bool(resp.get("ok"))
    return web.json_response({
        "ok": ok,
        "active_character": resp.get("active_character"),
        "name": resp.get("name"),
        "director_enabled": resp.get("director_enabled"),
        "error": resp.get("error"),
    }, status=200 if ok else 400)


async def handle_user_clear_chat(request: web.Request) -> web.Response:
    """POST /v1/clear_chat — ask the ST extension to start a fresh chat for the active character."""
    if not _check_user_auth(request):
        return web.Response(status=401, text="Unauthorized")
    request_id = str(uuid.uuid4())
    if not await broadcast_to_st({"type": "clear_chat", "id": request_id}):
        return web.Response(status=503, text="No ST extension connected")
    resp = await _await_single_response(request_id, SETTINGS["request_timeout_s"])
    if resp is None:
        return web.Response(status=504, text="timeout waiting for ST (clear_chat)")
    ok = bool(resp.get("ok"))
    return web.json_response({"ok": ok, "error": resp.get("error")}, status=200 if ok else 400)


async def _blocking_user_response(request_id: str) -> web.Response:
    """Accumulate st_stream deltas; st_response content replaces the accumulated text
    (final ST message is authoritative — handles cases where streaming missed chars)."""
    queue: asyncio.Queue = asyncio.Queue()
    _response_futures[request_id] = queue
    chunks: list[str] = []
    final_content: str | None = None
    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=SETTINGS["request_timeout_s"])
            except asyncio.TimeoutError:
                log.warning("blocking user request timeout id=%s", request_id)
                return web.Response(status=504, text="timeout waiting for ST")

            event = data.get("type")
            if event == "st_stream":
                delta = data.get("delta") or ""
                if delta and final_content is None:
                    chunks.append(delta)
            elif event == "st_response":
                final_content = (data.get("content") or "").strip()
                break
        content = final_content if final_content is not None else "".join(chunks).strip()
        return web.json_response({
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion",
            "created": int(asyncio.get_event_loop().time()),
            "model": "st-bridge",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })
    finally:
        _response_futures.pop(request_id, None)


async def _stream_user_response(request: web.Request, request_id: str) -> web.StreamResponse:
    queue: asyncio.Queue = asyncio.Queue()
    _response_futures[request_id] = queue

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    full_text: list[str] = []
    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=SETTINGS["request_timeout_s"])
            except asyncio.TimeoutError:
                log.warning("stream user request timeout id=%s", request_id)
                await response.write(b"data: [DONE]\n\n")
                break

            event = data.get("type")
            if event == "st_stream":
                delta = data.get("delta") or ""
                if delta:
                    full_text.append(delta)
                    chunk = {
                        "id": f"chatcmpl-{request_id}",
                        "object": "chat.completion.chunk",
                        "model": "st-bridge",
                        "choices": [{
                            "index": 0,
                            "delta": {"content": delta},
                            "finish_reason": None,
                        }],
                    }
                    await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
            elif event == "st_response":
                tail = data.get("content") or ""
                if tail and tail not in "".join(full_text):
                    full_text.append(tail)
                    chunk = {
                        "id": f"chatcmpl-{request_id}",
                        "object": "chat.completion.chunk",
                        "model": "st-bridge",
                        "choices": [{
                            "index": 0,
                            "delta": {"content": tail},
                            "finish_reason": None,
                        }],
                    }
                    await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
                # send final stop
                stop = {
                    "id": f"chatcmpl-{request_id}",
                    "object": "chat.completion.chunk",
                    "model": "st-bridge",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                await response.write(f"data: {json.dumps(stop)}\n\n".encode())
                await response.write(b"data: [DONE]\n\n")
                break
    finally:
        _response_futures.pop(request_id, None)

    return response


# ---------- App factory ----------


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/models", handle_user_models)
    app.router.add_get("/models", handle_user_models)
    app.router.add_post("/v1/chat/completions", handle_user_chat_completions)
    app.router.add_get("/v1/characters", handle_user_characters)
    app.router.add_post("/v1/select_character", handle_user_select_character)
    app.router.add_post("/v1/clear_chat", handle_user_clear_chat)
    app.router.add_get("/healthz", lambda _r: web.json_response({"ok": True}))
    return app


async def run_websocket_server() -> websockets.WebSocketServer:
    server = await websockets.serve(
        ws_handler,
        SETTINGS["websocket"]["host"],
        SETTINGS["websocket"]["port"],
    )
    log.info("WebSocket server: ws://%s:%d",
             SETTINGS["websocket"]["host"], SETTINGS["websocket"]["port"])
    return server


async def run_http_server() -> web.AppRunner:
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(
        runner,
        SETTINGS["user_api"]["host"],
        SETTINGS["user_api"]["port"],
    )
    await site.start()
    log.info("User API: http://%s:%d",
             SETTINGS["user_api"]["host"], SETTINGS["user_api"]["port"])
    return runner


async def async_main() -> None:
    ws_server = await run_websocket_server()
    http_runner = await run_http_server()
    log.info("bridge up; press Ctrl+C to stop")
    try:
        await asyncio.Future()  # park forever
    finally:
        ws_server.close()
        await http_runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="SillyTavern bridge for V2 Telegram bot")
    parser.add_argument("--print-config", action="store_true",
                        help="print effective config and exit")
    args = parser.parse_args()
    if args.print_config:
        print(json.dumps(DEFAULT_SETTINGS, indent=2))
        return
    SETTINGS.update(DEFAULT_SETTINGS)
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()