"""Command line: python -m tavern {run|check|init|bots} [--data-dir DIR]"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import urllib.request
from pathlib import Path

from .config import default_data_dir, ensure_data_dir, load_bots, validate_bot
from .manager import Engine, setup_logging


def check_ollama(url: str) -> tuple[bool, list[str]]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=4) as r:
            data = json.load(r)
        return True, [m.get("name", "") for m in data.get("models", [])]
    except Exception:  # noqa: BLE001
        return False, []


def cmd_check(data_dir: Path) -> int:
    ensure_data_dir(data_dir)
    bots = load_bots(data_dir)
    print(f"data dir: {data_dir}")
    urls = {b.ollama_url for b in bots} or {"http://127.0.0.1:11434"}
    models: dict[str, list[str]] = {}
    for u in urls:
        ok, names = check_ollama(u)
        models[u] = names
        print(f"ollama {u}: {'OK, ' + str(len(names)) + ' models' if ok else 'NOT REACHABLE'}")
    rc = 0
    for b in bots:
        problems = validate_bot(b, data_dir)
        for needed in (b.model, b.extract_model if (b.mode == "rpg" and b.extract) else None):
            if needed and models.get(b.ollama_url) and needed not in models[b.ollama_url]:
                problems.append(f"模型未安装: {needed}（ollama pull）")
        flag = "disabled" if not b.enabled else ("OK" if not problems else "PROBLEM")
        print(f"- {b.name} [{b.mode}] {flag}")
        for p in problems:
            print(f"    · {p}")
            if b.enabled and not p.startswith("rpg 模式只使用"):
                rc = 1
    return rc


def cmd_run(data_dir: Path) -> int:
    ensure_data_dir(data_dir)
    setup_logging(data_dir)
    engine = Engine(data_dir)
    stop = asyncio.Event()

    async def main():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await engine.run(stop)

    asyncio.run(main())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tavern")
    ap.add_argument("--data-dir", default=None, help="default: $TAVERN_DATA_DIR or ~/Library/Application Support/TelegramTavern")
    ap.add_argument("cmd", choices=["run", "check", "init", "bots"])
    a = ap.parse_args(argv)
    data_dir = Path(a.data_dir).expanduser().resolve() if a.data_dir else default_data_dir()
    if a.cmd == "init":
        ensure_data_dir(data_dir)
        print(f"initialised {data_dir}")
        return 0
    if a.cmd == "bots":
        for b in load_bots(ensure_data_dir(data_dir)):
            print(f"{b.name}\t{b.mode}\t{'on' if b.enabled else 'off'}\t{','.join(b.character_files)}")
        return 0
    if a.cmd == "check":
        return cmd_check(data_dir)
    return cmd_run(data_dir)


if __name__ == "__main__":
    sys.exit(main())
