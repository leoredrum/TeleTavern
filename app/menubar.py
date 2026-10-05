"""TelegramTavern — macOS menu-bar app around the tavern engine.

Menu:  status · Start/Stop · Bots (enable/disable) · Ollama (check / pull model /
install) · Open data folder · Open logs · Reload · Quit.

The engine runs in a background thread with its own asyncio loop; the data
directory defaults to ~/Library/Application Support/TelegramTavern.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import rumps
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # running from source
    sys.path.insert(0, str(ROOT))

from tavern.cli import check_ollama  # noqa: E402
from tavern.config import default_data_dir, ensure_data_dir, load_bots, validate_bot  # noqa: E402
from tavern.manager import Engine, setup_logging  # noqa: E402

ICON_IDLE, ICON_RUN = "🍺", "🍻"


class EngineThread:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.engine: Engine | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        if self.alive:
            return
        self.engine = Engine(self.data_dir)

        def runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.stop_event = asyncio.Event()
            try:
                self.loop.run_until_complete(self.engine.run(self.stop_event))
            finally:
                self.loop.close()

        self.thread = threading.Thread(target=runner, name="tavern-engine", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.alive and self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)
            self.thread.join(timeout=15)


class TavernApp(rumps.App):
    def __init__(self):
        super().__init__("TelegramTavern", title=ICON_IDLE, quit_button=None)
        self.data_dir = ensure_data_dir(default_data_dir())
        setup_logging(self.data_dir)
        self.engine = EngineThread(self.data_dir)
        self.status_item = rumps.MenuItem("状态：已停止")
        self.toggle_item = rumps.MenuItem("启动全部 bot", callback=self.toggle)
        self.bots_menu = rumps.MenuItem("Bots")
        self.ollama_menu = rumps.MenuItem("Ollama")
        self.ollama_menu.update([
            rumps.MenuItem("检查 Ollama 与模型", callback=self.check_ollama),
            rumps.MenuItem("拉取模型…", callback=self.pull_model),
            rumps.MenuItem("安装 Ollama（打开官网）", callback=lambda _: webbrowser.open("https://ollama.com/download")),
        ])
        self.menu = [
            self.status_item, None,
            self.toggle_item,
            self.bots_menu,
            self.ollama_menu, None,
            rumps.MenuItem("打开数据目录（角色卡 / 世界书 / bots）", callback=self.open_data),
            rumps.MenuItem("打开日志", callback=self.open_logs),
            rumps.MenuItem("重新加载配置", callback=self.reload),
            None,
            rumps.MenuItem("退出", callback=self.quit),
        ]
        self.refresh_bots()
        self.timer = rumps.Timer(self.tick, 3)
        self.timer.start()
        if os.environ.get("TAVERN_AUTOSTART", "1") != "0":
            self.toggle(None)

    # ---- bots submenu --------------------------------------------------------------
    def refresh_bots(self) -> None:
        if getattr(self.bots_menu, "_menu", None) is not None:   # rumps: clear() needs an existing submenu
            self.bots_menu.clear()
        bots = load_bots(self.data_dir)
        if not bots:
            self.bots_menu.add(rumps.MenuItem("（bots/ 目录为空 — 放入 yaml 后重新加载）"))
        for b in bots:
            problems = [p for p in validate_bot(b, self.data_dir) if not p.startswith("rpg 模式只使用")]
            label = f"{'✅' if b.enabled else '⬜'} {b.name} [{b.mode}]" + (" ⚠️" if problems else "")
            item = rumps.MenuItem(label, callback=self.make_toggle_bot(b.path))
            if problems:
                sub = rumps.MenuItem("问题")
                for p in problems:
                    sub.add(rumps.MenuItem(p))
                item.add(sub)
            self.bots_menu.add(item)

    def make_toggle_bot(self, path: Path):
        def _cb(_sender):
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            raw["enabled"] = not raw.get("enabled", True)
            path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
            self.refresh_bots()
            rumps.notification("TelegramTavern", "bot 配置已修改", f"{path.stem}: enabled={raw['enabled']}（重启引擎生效）")
        return _cb

    # ---- engine ---------------------------------------------------------------------------
    def toggle(self, _sender) -> None:
        if self.engine.alive:
            self.engine.stop()
            self.title = ICON_IDLE
            self.toggle_item.title = "启动全部 bot"
        else:
            self.engine.start()
            self.title = ICON_RUN
            self.toggle_item.title = "停止全部 bot"

    def tick(self, _timer) -> None:
        if not self.engine.alive:
            self.status_item.title = "状态：已停止"
            self.title = ICON_IDLE
            self.toggle_item.title = "启动全部 bot"
            return
        st = self.engine.engine.status if self.engine.engine else {}
        running = sum(1 for v in st.values() if v.startswith("running"))
        self.status_item.title = f"状态：{running} 个 bot 运行中" + (
            "；" + "；".join(f"{k}: {v}" for k, v in st.items() if not v.startswith("running") and v != "disabled")
            if any(not v.startswith("running") and v != "disabled" for v in st.values()) else "")

    def reload(self, _sender) -> None:
        was = self.engine.alive
        if was:
            self.engine.stop()
        self.refresh_bots()
        if was:
            self.engine.start()
        rumps.notification("TelegramTavern", "已重新加载", f"{self.data_dir}")

    # ---- ollama --------------------------------------------------------------------------------
    def check_ollama(self, _sender) -> None:
        bots = load_bots(self.data_dir)
        url = bots[0].ollama_url if bots else "http://127.0.0.1:11434"
        ok, names = check_ollama(url)
        if not ok:
            if shutil.which("ollama"):
                subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                rumps.notification("TelegramTavern", "Ollama 未运行", "已尝试启动 ollama serve，几秒后再检查。")
            else:
                rumps.notification("TelegramTavern", "未安装 Ollama", "请从 Ollama 菜单打开官网安装。")
            return
        needed = {b.model for b in bots if b.enabled} | {b.extract_model for b in bots if b.enabled and b.mode == "rpg"}
        missing = sorted(m for m in needed if m not in names)
        rumps.notification("TelegramTavern", f"Ollama 正常，{len(names)} 个模型",
                           "缺少：" + ", ".join(missing) if missing else "所有 bot 需要的模型都已安装。")

    def pull_model(self, _sender) -> None:
        bots = load_bots(self.data_dir)
        default = next((b.model for b in bots), "")
        w = rumps.Window("输入要拉取的 Ollama 模型名", "拉取模型", default_text=default, ok="拉取", cancel="取消")
        r = w.run()
        if not r.clicked or not r.text.strip():
            return
        model = r.text.strip()

        def work():
            proc = subprocess.run(["ollama", "pull", model], capture_output=True, text=True)
            rumps.notification("TelegramTavern", f"拉取 {model}", "完成" if proc.returncode == 0 else proc.stderr[-200:])

        threading.Thread(target=work, daemon=True).start()
        rumps.notification("TelegramTavern", f"开始拉取 {model}", "完成后会通知。")

    # ---- misc ------------------------------------------------------------------------------------
    def open_data(self, _sender) -> None:
        subprocess.Popen(["open", str(self.data_dir)])

    def open_logs(self, _sender) -> None:
        subprocess.Popen(["open", str(self.data_dir / "logs")])

    def quit(self, _sender) -> None:
        self.engine.stop()
        rumps.quit_application()


if __name__ == "__main__":
    TavernApp().run()
