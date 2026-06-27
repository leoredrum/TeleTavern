"""
Headless ST extension simulator for end-to-end smoke testing.

Connects to the bridge's WebSocket as if it were the ST extension, receives
user_request frames, and replies with a canned st_response. Lets us verify
the bridge → extension → bridge round-trip without an actual ST browser.

Usage:
    ../venv/bin/python tests/headless_extension.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
WS_URL = os.environ.get("WS_URL", "ws://127.0.0.1:8001")


async def main() -> None:
    print(f"connecting {WS_URL} ...")
    async with websockets.connect(WS_URL) as ws:
        print("connected; waiting for user_request ...")
        async for raw in ws:
            data = json.loads(raw)
            t = data.get("type")
            if t != "user_request":
                print(f"recv non-request: {data}")
                continue
            request_id = data["id"]
            oai_body = data.get("content") or {}
            last_user = ""
            for m in (oai_body.get("messages") or []):
                if m.get("role") == "user":
                    last_user = m.get("content", "")
            print(f"got user_request id={request_id} user_text={last_user[:80]!r}")

            # Simulate ST: send a chunk + final response.
            await ws.send(json.dumps({
                "type": "st_stream",
                "id": request_id,
                "delta": "（模拟 ST 回复）",
            }))
            await asyncio.sleep(0.05)
            await ws.send(json.dumps({
                "type": "st_response",
                "id": request_id,
                "content": f"（模拟 ST 回复）你说的是：{last_user}",
            }))
            print(f"sent st_response id={request_id}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)