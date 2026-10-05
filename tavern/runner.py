"""EngineThread — run the Engine in a background thread with its own asyncio loop.

Shared by the menu-bar shell and the window app.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from .manager import Engine


class EngineThread:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.engine: Engine | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.thread: threading.Thread | None = None
        self.last_error: str = ""

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    @property
    def status(self) -> dict[str, str]:
        return dict(self.engine.status) if self.engine else {}

    def start(self) -> None:
        if self.alive:
            return
        self.last_error = ""
        self.engine = Engine(self.data_dir)

        def runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.stop_event = asyncio.Event()
            try:
                self.loop.run_until_complete(self.engine.run(self.stop_event))
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
            finally:
                self.loop.close()

        self.thread = threading.Thread(target=runner, name="tavern-engine", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 15) -> None:
        if self.alive and self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)
            self.thread.join(timeout=timeout)

    def restart(self) -> None:
        self.stop()
        self.start()
