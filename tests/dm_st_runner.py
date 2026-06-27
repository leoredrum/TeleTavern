"""
Persistent headless ST WebUI runner.

The SillyTavern ChatBridge ST extension lives inside the browser session.
When the browser closes, the WebSocket to the bridge drops and the V2
Telegram bot starts getting HTTP 503 ("No ST extension connected").

This script keeps a headless Chromium instance alive with ST WebUI loaded,
so the extension stays connected 24/7. It also restarts the browser if
ST itself crashes or the page hangs.

LaunchAgent-friendly: runs forever, writes logs, writes pidfile.

Run:
    ./venv/bin/python tests/persistent_st_runner.py
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

ST_URL = os.environ.get("ST_URL", "http://127.0.0.1:8010")
ST_HEALTH_TIMEOUT_S = 30
WS_STATUS_QUERY = """
async () => {
    const el = document.querySelector('#chatbridge_ws_status');
    return el ? (el.textContent || '').trim() : 'no-element';
}
"""
HERE = Path(__file__).resolve().parent.parent
# No LOG_FILE constant — stdout is the log sink (LaunchAgent / nohup redirect it).
PID_FILE = HERE / "logs" / "dm-st-runner.pid"


def log(msg: str) -> None:
    # Only write to stdout — when stdout is redirected (LaunchAgent / nohup
    # `>> file`), the redirect is the canonical sink. Writing both to file
    # AND stdout when stdout is file-bound produces duplicate log lines.
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(f"[{ts}] {msg}\n")
    sys.stdout.flush()


async def check_st_alive() -> bool:
    """Probe ST's HTTP endpoint."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(ST_URL.rstrip("/") + "/", timeout=aiohttp.ClientTimeout(total=5)) as r:
                return r.status == 200
    except Exception:
        return False


async def main() -> int:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    log(f"st-runner started pid={os.getpid()}, watching {ST_URL}")

    # Signal handlers for clean shutdown
    stop_event = asyncio.Event()

    def _signal(_sig, _frm):
        log("received signal, stopping")
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    backoff = 5  # seconds, capped

    while not stop_event.is_set():
        # Verify ST is up before launching browser
        if not await check_st_alive():
            log(f"ST not reachable at {ST_URL}, sleeping 10s")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
                break
            except asyncio.TimeoutError:
                continue

        try:
            async with async_playwright() as pw:
                # Use headless=True with persistent context; persistent keeps
                # the chat history and extension state across restarts.
                user_data_dir = Path.home() / ".st-runner-dm-profile"
                user_data_dir.mkdir(parents=True, exist_ok=True)
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                    viewport={"width": 1280, "height": 800},
                )
                page = await context.new_page()

                def _on_console(msg):
                    if msg.type in ("error", "warning"):
                        log(f"[browser:{msg.type}] {msg.text[:300]}")

                page.on("console", _on_console)

                # Block ST WebSocket reconnect noise from flooding logs
                log(f"navigating to {ST_URL}")
                try:
                    await page.goto(ST_URL, wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:
                    log(f"navigation error: {exc}")

                # Wait for ChatBridge extension to connect
                ws_connected = False
                for attempt in range(60):  # up to 60s
                    if stop_event.is_set():
                        break
                    try:
                        status = await asyncio.wait_for(
                            page.evaluate(WS_STATUS_QUERY), timeout=5
                        )
                    except Exception as exc:
                        status = f"(error: {exc})"

                    if status == "已连接":
                        if not ws_connected:
                            log(f"ChatBridge WS connected (attempt {attempt})")
                            ws_connected = True
                    elif ws_connected:
                        log(f"ChatBridge WS dropped: {status}")

                    if ws_connected:
                        # Healthy — sleep 30s before next check
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=30)
                            break
                        except asyncio.TimeoutError:
                            pass
                    else:
                        # Still waiting — sleep 1s
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=1)
                            break
                        except asyncio.TimeoutError:
                            pass

                # WS was never connected or page closed; exit context to relaunch
                await context.close()
                backoff = min(backoff * 2, 60)
                log(f"restarting browser in {backoff}s")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    pass
        except Exception as exc:
            log(f"browser loop error: {exc}")
            backoff = min(backoff * 2, 60)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = 5

    PID_FILE.unlink(missing_ok=True)
    log("st-runner exiting")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)