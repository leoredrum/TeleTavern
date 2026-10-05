"""TelegramTavern — desktop window app (pywebview) around the tavern engine.

Sections: 总览 / Bots / 角色卡 / 世界书 / 模型 / 日志. The HTML lives in app/ui.html
and talks to the `Api` class below through window.pywebview.api.*.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import webview
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tavern.config import (DEFAULT_MODEL, BotConfig, default_data_dir, ensure_data_dir,  # noqa: E402
                           load_bots, validate_bot)
from tavern.manager import setup_logging  # noqa: E402
from tavern.local import LocalChat  # noqa: E402
from tavern.runner import EngineThread  # noqa: E402
from tavern.worldinfo import entries_from_character_book, load_world_file  # noqa: E402

_TOKEN_KEY = re.compile(r"^[A-Z0-9_]{3,64}$")

BOT_FIELDS = ["name", "enabled", "mode", "token_env", "characters", "worlds", "model", "extract_model",
              "extract", "rules", "scenes", "reply_language", "user_label", "max_context", "max_tokens",
              "history_limit", "temperature", "director_characters", "story_engine", "language_override",
              "newgame_prompt", "continue_prompt", "translation_table", "first_mes_translate"]


def _ui_path() -> str:
    base = Path(getattr(sys, "_MEIPASS", ROOT / "app"))
    p = base / "ui.html"
    if not p.exists():
        p = ROOT / "app" / "ui.html"
    return str(p)


class Api:
    def __init__(self, data_dir: Path):
        self.data_dir = ensure_data_dir(data_dir)
        self.engine = EngineThread(self.data_dir)
        self._thumbs: dict[str, str] = {}
        self._pull: dict[str, str] = {}
        self.window = None
        self.local = LocalChat(self.data_dir)

    # ---- state ------------------------------------------------------------------------
    def state(self) -> dict:
        bots = []
        status = self.engine.status
        for b in load_bots(self.data_dir):
            problems = [p for p in validate_bot(b, self.data_dir) if not p.startswith("rpg 模式只使用")]
            bots.append({"name": b.name, "mode": b.mode, "enabled": b.enabled, "model": b.model,
                         "characters": b.character_files, "worlds": b.worlds, "problems": problems,
                         "status": status.get(b.name, "stopped" if not self.engine.alive else "starting")})
        return {"running": self.engine.alive, "data_dir": str(self.data_dir), "bots": bots,
                "last_error": self.engine.last_error, "ollama": self.ollama()}

    def start(self) -> dict:
        self.engine.start()
        return {"ok": True}

    def stop(self) -> dict:
        self.engine.stop()
        return {"ok": True}

    def restart(self) -> dict:
        self.engine.restart()
        return {"ok": True}

    # ---- bots ------------------------------------------------------------------------------
    def _bot_path(self, name: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", name).strip("-") or "bot"
        return self.data_dir / "bots" / f"{safe}.yaml"

    def get_bot(self, name: str) -> dict:
        p = self._bot_path(name)
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        defaults = BotConfig(name=name)
        out = {}
        for f in BOT_FIELDS:
            out[f] = raw.get(f, getattr(defaults, f, None)) if isinstance(raw, dict) else getattr(defaults, f, None)
        out["name"] = name
        return out

    def save_bot(self, name: str, data: dict) -> dict:
        clean = {}
        defaults = BotConfig(name=name)
        for f in BOT_FIELDS:
            if f not in data:
                continue
            v = data[f]
            d = getattr(defaults, f, None)
            if isinstance(d, bool):
                v = bool(v)
            elif isinstance(d, int) and not isinstance(d, bool):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    v = d
            elif isinstance(d, float):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    v = d
            elif isinstance(d, list) and isinstance(v, str):
                v = [s.strip() for s in v.splitlines() if s.strip()]
            clean[f] = v
        clean["name"] = name
        tt = clean.get("translation_table")
        if isinstance(tt, list):
            pairs = []
            for row in tt:
                if isinstance(row, str) and "=" in row:
                    a, b = row.split("=", 1)
                    pairs.append([a.strip(), b.strip()])
                elif isinstance(row, (list, tuple)) and len(row) == 2:
                    pairs.append([str(row[0]), str(row[1])])
            clean["translation_table"] = pairs
        p = self._bot_path(name)
        p.write_text(yaml.safe_dump(clean, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
        self.local.reload(name)
        cfg = BotConfig(**{k: v for k, v in clean.items() if k in BotConfig.__dataclass_fields__})
        return {"ok": True, "problems": [x for x in validate_bot(cfg, self.data_dir) if "token" not in x.lower()
                                         or not cfg.token_env]}

    def new_bot(self, name: str, mode: str) -> dict:
        p = self._bot_path(name)
        if p.exists():
            return {"ok": False, "error": "同名 bot 已存在"}
        env_key = "TG_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
        cfg = {"name": name, "enabled": False, "mode": mode if mode in ("dialogue", "rpg") else "dialogue",
               "token_env": env_key, "characters": [], "worlds": [], "model": DEFAULT_MODEL,
               "reply_language": "zh-CN", "user_label": "你"}
        if cfg["mode"] == "rpg":
            cfg.update(extract_model="qwen3:14b", extract=True, rules=False, scenes=False, max_tokens=4096)
        p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return {"ok": True, "name": name}

    def delete_bot(self, name: str) -> dict:
        p = self._bot_path(name)
        if p.exists():
            p.unlink()
        return {"ok": True}

    def toggle_bot(self, name: str) -> dict:
        p = self._bot_path(name)
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        raw["enabled"] = not raw.get("enabled", True)
        p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
        return {"ok": True, "enabled": raw["enabled"]}

    # ---- tokens (.env) -----------------------------------------------------------------------
    def _env_lines(self) -> list[str]:
        p = self.data_dir / ".env"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def tokens(self) -> list[dict]:
        out = []
        for line in self._env_lines():
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
            if m and m.group(1).startswith("TG_TOKEN_"):
                val = m.group(2).strip().strip('"')
                out.append({"key": m.group(1), "set": bool(val), "hint": (val[:4] + "…" + val[-3:]) if len(val) > 10 else ""})
        return out

    def set_token(self, key: str, value: str) -> dict:
        key = key.strip()
        if not _TOKEN_KEY.match(key) or not key.startswith("TG_TOKEN_"):
            return {"ok": False, "error": "key 必须形如 TG_TOKEN_XXX"}
        value = (value or "").strip()
        lines = self._env_lines()
        done = False
        for i, line in enumerate(lines):
            if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                lines[i] = f"{key}={value}"
                done = True
        if not done:
            lines.append(f"{key}={value}")
        p = self.data_dir / ".env"
        p.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
        os.chmod(p, 0o600)
        return {"ok": True}

    # ---- characters / worlds ----------------------------------------------------------------------
    def characters(self) -> list[dict]:
        from character_card import load_character
        out = []
        for p in sorted((self.data_dir / "characters").glob("*.png")):
            item = {"file": p.name, "name": p.stem, "size_kb": p.stat().st_size // 1024, "book": 0, "greetings": 0,
                    "thumb": "", "error": ""}
            try:
                c = load_character(p)
                item.update(name=c.name or p.stem, book=len((c.character_book or {}).get("entries", []) or []),
                            greetings=1 + len(c.alternate_greetings or []),
                            desc=(c.creator_notes or c.description or "").replace("\n", " ")[:120])
            except Exception as exc:  # noqa: BLE001
                item["error"] = str(exc)[:80]
            item["thumb"] = self._thumb(p)
            out.append(item)
        return out

    def _thumb(self, p: Path) -> str:
        key = f"{p.name}:{p.stat().st_mtime_ns}"
        if key in self._thumbs:
            return self._thumbs[key]
        try:
            from PIL import Image
            im = Image.open(p)
            im.thumbnail((96, 128))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=80)
            uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:  # noqa: BLE001
            uri = ""
        self._thumbs[key] = uri
        return uri

    def worlds(self) -> list[dict]:
        out = []
        for p in sorted((self.data_dir / "worlds").glob("*.json")):
            try:
                ents = load_world_file(p)
                out.append({"file": p.name, "entries": len(ents), "constant": sum(1 for e in ents if e.constant),
                            "keys": sum(len(e.keys) for e in ents)})
            except Exception as exc:  # noqa: BLE001
                out.append({"file": p.name, "entries": 0, "constant": 0, "keys": 0, "error": str(exc)[:80]})
        return out

    def card_book_preview(self, file: str) -> dict:
        from character_card import load_character
        try:
            c = load_character(self.data_dir / "characters" / file)
            ents = entries_from_character_book(c.character_book, source=c.name)
            return {"ok": True, "name": c.name, "entries": [{"keys": e.keys[:6], "constant": e.constant,
                                                              "content": e.content[:160]} for e in ents[:60]]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def import_files(self, kind: str) -> dict:
        ext = ("*.png",) if kind == "characters" else ("*.json",)
        dest = self.data_dir / kind
        try:
            paths = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                                   file_types=(f"{kind} ({';'.join(ext)})",)) if self.window else None
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        copied = []
        for src in paths or []:
            try:
                shutil.copy2(src, dest / Path(src).name)
                copied.append(Path(src).name)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{src}: {exc}"}
        return {"ok": True, "copied": copied}

    # ---- ollama -------------------------------------------------------------------------------------
    def ollama(self) -> dict:
        bots = load_bots(self.data_dir)
        url = bots[0].ollama_url if bots else "http://127.0.0.1:11434"
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=3) as r:
                data = json.load(r)
            models = [{"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 1)} for m in data.get("models", [])]
            needed = sorted({b.model for b in bots if b.enabled} | {b.extract_model for b in bots if b.enabled and b.mode == "rpg" and b.extract})
            names = {m["name"] for m in models}
            return {"ok": True, "url": url, "models": models, "missing": [n for n in needed if n not in names],
                    "installed": bool(shutil.which("ollama")), "pulls": dict(self._pull)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "url": url, "models": [], "missing": [], "error": str(exc)[:120],
                    "installed": bool(shutil.which("ollama")), "pulls": dict(self._pull)}

    def ollama_serve(self) -> dict:
        if not shutil.which("ollama"):
            return {"ok": False, "error": "未安装 Ollama"}
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}

    def pull_model(self, name: str) -> dict:
        name = (name or "").strip()
        if not name or not shutil.which("ollama"):
            return {"ok": False, "error": "模型名为空或未安装 Ollama"}
        if self._pull.get(name, "").startswith("pulling"):
            return {"ok": True}
        self._pull[name] = "pulling 0%"

        def work():
            try:
                proc = subprocess.Popen(["ollama", "pull", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1)
                for line in proc.stdout or []:
                    m = re.search(r"(\d{1,3})%", line)
                    if m:
                        self._pull[name] = f"pulling {m.group(1)}%"
                proc.wait()
                self._pull[name] = "done" if proc.returncode == 0 else "failed"
            except Exception as exc:  # noqa: BLE001
                self._pull[name] = f"failed: {str(exc)[:80]}"

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    # ---- local chat ---------------------------------------------------------------------------------------
    def local_info(self, name: str) -> dict:
        try:
            info = self.local.info(name)
            info.update(self.local.history(name))
            return {"ok": True, **info}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

    def local_send(self, name: str, text: str) -> dict:
        self.local.send(name, text)
        return {"ok": True}

    def local_cmd(self, name: str, cmd: str, arg: str = "") -> dict:
        self.local.command(name, cmd, arg)
        return {"ok": True}

    def local_select(self, name: str, card_file: str) -> dict:
        self.local.select_character(name, card_file)
        return {"ok": True}

    def local_poll(self, name: str) -> dict:
        return self.local.convo(name).snapshot()

    def local_clear(self, name: str) -> dict:
        self.local.clear_view(name)
        return {"ok": True}

    def open_file(self, path: str) -> dict:
        if path and Path(path).exists():
            subprocess.Popen(["open", path])
        return {"ok": True}

    # ---- logs / misc --------------------------------------------------------------------------------------
    def logs(self, lines: int = 200) -> str:
        p = self.data_dir / "logs" / "engine.log"
        if not p.exists():
            return "（暂无日志）"
        try:
            data = p.read_bytes()[-200_000:].decode("utf-8", "ignore").splitlines()
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        return "\n".join(data[-lines:])

    def open_path(self, kind: str) -> dict:
        target = self.data_dir if kind == "root" else self.data_dir / kind
        subprocess.Popen(["open", str(target)])
        return {"ok": True}

    def open_url(self, url: str) -> dict:
        if url.startswith(("http://", "https://")):
            subprocess.Popen(["open", url])
        return {"ok": True}


def main() -> None:
    data_dir = ensure_data_dir(default_data_dir())
    setup_logging(data_dir)
    api = Api(data_dir)
    window = webview.create_window("Telegram Tavern", _ui_path(), js_api=api, width=1180, height=780,
                                   min_size=(900, 600))
    api.window = window
    if os.environ.get("TAVERN_AUTOSTART", "1") != "0":
        api.engine.start()

    def on_closing():
        api.engine.stop(timeout=10)

    window.events.closing += on_closing
    webview.start(debug=os.environ.get("TAVERN_DEBUG") == "1")


if __name__ == "__main__":
    main()
