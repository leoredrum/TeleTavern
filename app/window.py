"""TelegramTavern — desktop window app (pywebview) around the tavern engine.

Sections: 总览 / Bots / 角色卡 / 世界书 / 模型 / 日志. The HTML lives in app/ui.html
and talks to the `Api` class below through window.pywebview.api.*.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import webview
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tavern.config import (DEFAULT_MODEL, BotConfig, default_data_dir, ensure_data_dir,  # noqa: E402
                           load_bots, validate_bot)
from tavern.manager import hot_add_character, setup_logging  # noqa: E402
from tavern import models as MODELS  # noqa: E402
from tavern.local import LocalChat  # noqa: E402
from tavern.runner import EngineThread  # noqa: E402
from tavern.store import StoreClient  # noqa: E402
from tavern.worldinfo import entries_from_character_book, load_world_file  # noqa: E402

_TOKEN_KEY = re.compile(r"^[A-Z0-9_]{3,64}$")

BOT_FIELDS = ["name", "enabled", "mode", "kind", "token_env", "characters", "worlds", "model", "extract_model",
              "extract", "rules", "scenes", "reply_language", "user_label", "max_context", "max_tokens",
              "history_limit", "temperature", "director_characters", "story_engine", "language_override",
              "newgame_prompt", "continue_prompt", "translation_table", "first_mes_translate"]


def _ui_path() -> str:
    base = Path(getattr(sys, "_MEIPASS", ROOT / "app"))
    p = base / "ui.html"
    if not p.exists():
        p = ROOT / "app" / "ui.html"
    return str(p)


def _reveal(target: str) -> None:
    """Open a file / folder / URL with the OS default handler, cross-platform."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", target])
    elif sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", target])


class Api:
    def __init__(self, data_dir: Path):
        self.data_dir = ensure_data_dir(data_dir)
        self.engine = EngineThread(self.data_dir)
        self._thumbs: dict[str, str] = {}
        self._pull: dict[str, dict] = {}
        self.window = None
        self.local = LocalChat(self.data_dir)
        self.store = StoreClient(self.data_dir)
        self._login_win = None
        self._dl_jobs: dict[int, dict] = {}
        self._dl_thread = None
        self._model_jobs: dict[str, dict] = {}

    # ---- state ------------------------------------------------------------------------
    def state(self) -> dict:
        bots = []
        status = self.engine.status
        for b in load_bots(self.data_dir):
            problems = [p for p in validate_bot(b, self.data_dir) if not p.startswith("rpg 模式只使用")]
            bots.append({"name": b.name, "mode": b.mode, "kind": b.kind, "enabled": b.enabled, "model": b.model,
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

    def new_bot(self, name: str, mode: str, kind: str = "telegram") -> dict:
        p = self._bot_path(name)
        if p.exists():
            return {"ok": False, "error": "同名 bot 已存在"}
        kind = kind if kind in ("telegram", "local") else "telegram"
        env_key = ("TG_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()) if kind == "telegram" else ""
        cfg = {"name": name, "enabled": kind == "local", "mode": mode if mode in ("dialogue", "rpg") else "dialogue",
               "kind": kind, "token_env": env_key, "characters": [], "worlds": [], "model": DEFAULT_MODEL,
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
                    "installed": bool(self._ollama_bin()), "pulls": dict(self._pull)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "url": url, "models": [], "missing": [], "error": str(exc)[:120],
                    "installed": bool(self._ollama_bin()), "pulls": dict(self._pull)}

    @staticmethod
    def _ollama_bin() -> str | None:
        """Locate the ollama executable. shutil.which first, then common install paths —
        a GUI .app launched from Finder/LaunchServices does not inherit the shell PATH,
        so /opt/homebrew/bin (Homebrew) is usually missing."""
        p = shutil.which("ollama")
        if p:
            return p
        cands = ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama", "/opt/local/bin/ollama"]
        if sys.platform == "win32":
            cands += [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
                      os.path.expandvars(r"%ProgramFiles%\Ollama\ollama.exe")]
        for cand in cands:
            if os.path.exists(cand):
                return cand
        return None

    def _ollama_url(self) -> str:
        bots = load_bots(self.data_dir)
        return (bots[0].ollama_url if bots else "http://127.0.0.1:11434").rstrip("/")

    def _do_pull(self, name: str, on_update) -> tuple[bool, str]:
        """Pull a model via the Ollama HTTP API (/api/pull, streaming). Independent of the
        ollama CLI / PATH. Calls on_update(percent:int, detail:str) as bytes arrive.
        Returns (ok, error)."""
        url = self._ollama_url() + "/api/pull"
        body = json.dumps({"model": name, "stream": True}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=None) as resp:
                for raw in resp:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    if ev.get("error"):
                        return False, str(ev["error"])[:120]
                    total = ev.get("total") or 0
                    completed = ev.get("completed") or 0
                    pct = int(completed * 100 / total) if total else 0
                    status = ev.get("status") or ""
                    on_update(pct, status)
                    if status == "success":
                        return True, ""
            return True, ""
        except urllib.error.HTTPError as exc:  # noqa: BLE001
            detail = ""
            try:
                detail = exc.read().decode()[:120]
            except Exception:  # noqa: BLE001
                pass
            return False, f"HTTP {exc.code} {detail}".strip()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:120]

    def ollama_serve(self) -> dict:
        binp = self._ollama_bin()
        if not binp:
            return {"ok": False, "error": "未找到 ollama 可执行文件"}
        subprocess.Popen([binp, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}

    def pull_model(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "模型名为空"}
        cur = self._pull.get(name)
        if isinstance(cur, dict) and cur.get("state") == "pulling":
            return {"ok": True}
        self._pull[name] = {"state": "pulling", "percent": 0, "detail": "准备中…"}

        def work():
            def upd(pct, detail):
                self._pull[name] = {"state": "pulling", "percent": pct, "detail": detail or f"{pct}%"}
            ok, err = self._do_pull(name, upd)
            self._pull[name] = ({"state": "done", "percent": 100, "detail": "完成"} if ok
                                else {"state": "failed", "percent": 0, "detail": err or "拉取失败"})

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
        try:
            n = self.local.clear(name)
            return {"ok": True, "cleared": n}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

    def open_file(self, path: str) -> dict:
        if path and Path(path).exists():
            _reveal(path)
        return {"ok": True}

    # ---- character-card store (aicharactercards.com API) ----------------------------------------
    LOGIN_URL = "https://aicharactercards.com/login"

    def store_auth(self) -> dict:
        u = self.store.me() if self.store.token else None
        return {"logged_in": bool(self.store.token and u), "user": self.store.user,
                "login_open": self._login_win is not None}

    def store_login(self) -> dict:
        """Open the site's login page; poll localStorage for the JWT, save it, close."""
        if self._login_win is not None:
            try:
                self._login_win.show()
                return {"ok": True, "reused": True}
            except Exception:  # noqa: BLE001
                self._login_win = None
        try:
            self._login_win = webview.create_window("登录 aicharactercards.com", self.LOGIN_URL, width=1000,
                                                    height=780, min_size=(700, 500))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

        def _closed():
            self._login_win = None

        self._login_win.events.closed += _closed

        def poll():
            for _ in range(600):      # up to ~10 minutes
                time.sleep(1)
                win = self._login_win
                if win is None:
                    return
                try:
                    tok = win.evaluate_js("localStorage.getItem('token')")
                    if tok and isinstance(tok, str) and len(tok) > 20:
                        user_raw = win.evaluate_js("localStorage.getItem('user')") or "{}"
                        try:
                            user = json.loads(user_raw) if isinstance(user_raw, str) else {}
                        except Exception:  # noqa: BLE001
                            user = {}
                        self.store.save_auth(tok, {k: user.get(k) for k in ("id", "username", "email") if isinstance(user, dict)})
                        self.store.me()
                        time.sleep(0.8)
                        try:
                            win.destroy()
                        except Exception:  # noqa: BLE001
                            pass
                        self._login_win = None
                        return
                except Exception:  # noqa: BLE001
                    continue

        threading.Thread(target=poll, name="tavern-store-login", daemon=True).start()
        return {"ok": True}

    def store_logout(self) -> dict:
        self.store.logout()
        return {"ok": True}

    def store_meta(self) -> dict:
        try:
            return {"ok": True, "tags": self.store.tags(), "languages": self.store.languages()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200], "tags": [], "languages": []}

    def store_browse(self, params: dict) -> dict:
        try:
            p = params or {}
            res = self.store.browse(source=p.get("source", "recent"), search=p.get("search", ""),
                                    language=p.get("language", ""), tags=p.get("tags") or [],
                                    nsfw=p.get("nsfw", ""), sort=p.get("sort", ""),
                                    limit=int(p.get("limit", 24)), skip=int(p.get("skip", 0)),
                                    period=p.get("period", "7d"), range_=p.get("range", "month"))
            have = {f.name for f in (self.data_dir / "characters").glob("*.png")}
            for it in res["items"]:
                it["owned"] = any(f"[aicc-{it['id']}]" in n for n in have)
            return {"ok": True, **res}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200], "items": [], "total": 0}

    def store_download(self, items: list) -> dict:
        """Queue downloads (sequential, rate-limit aware). items: [{id, title}]"""
        queued = 0
        for it in items or []:
            cid = int(it.get("id"))
            if cid in self._dl_jobs and self._dl_jobs[cid]["state"] in ("queued", "downloading"):
                continue
            self._dl_jobs[cid] = {"id": cid, "title": it.get("title", ""), "state": "queued", "file": "", "error": ""}
            queued += 1
        if queued and not self._dl_thread_alive():
            self._dl_thread = threading.Thread(target=self._dl_worker, name="tavern-store-dl", daemon=True)
            self._dl_thread.start()
        return {"ok": True, "queued": queued}

    def _dl_thread_alive(self) -> bool:
        return bool(self._dl_thread and self._dl_thread.is_alive())

    def _dl_worker(self) -> None:
        while True:
            job = next((j for j in self._dl_jobs.values() if j["state"] == "queued"), None)
            if job is None:
                return
            job["state"] = "downloading"
            try:
                path = self.store.download(job["id"], self.data_dir / "characters", title=job["title"])
                job.update(state="done", file=path.name)
                self._thumbs.pop(path.name, None)
            except Exception as exc:  # noqa: BLE001
                job.update(state="failed", error=str(exc)[:160])
            time.sleep(1.5)        # be gentle with the rate limiter

    def store_progress(self) -> dict:
        jobs = list(self._dl_jobs.values())
        return {"jobs": jobs[-50:], "active": any(j["state"] in ("queued", "downloading") for j in jobs)}

    def store_clear_done(self) -> dict:
        self._dl_jobs = {k: v for k, v in self._dl_jobs.items() if v["state"] in ("queued", "downloading")}
        return {"ok": True}

    # ---- one-click deploy --------------------------------------------------------------------------------
    def deploy_targets(self) -> dict:
        bots = load_bots(self.data_dir)
        return {"bots": [{"name": b.name, "mode": b.mode, "kind": b.kind, "enabled": b.enabled, "characters": b.character_files,
                          "has_token": bool(b.token)} for b in bots]}

    def deploy_card(self, card_file: str, target: dict) -> dict:
        """target = {"bot": "<existing name>"}  or  {"new": {"name", "mode", "token"}}"""
        card_path = self.data_dir / "characters" / card_file
        if not card_path.exists():
            return {"ok": False, "error": f"角色卡不存在: {card_file}"}
        target = target or {}
        try:
            if target.get("new"):
                n = target["new"]
                name = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "-", (n.get("name") or "").strip()).strip("-")
                if not name:
                    return {"ok": False, "error": "请填写 bot 名称"}
                if self._bot_path(name).exists():
                    return {"ok": False, "error": "同名 bot 已存在，请改名或选择「部署到现有 bot」"}
                mode = n.get("mode") if n.get("mode") in ("dialogue", "rpg") else "dialogue"
                kind = "local" if n.get("kind") == "local" else "telegram"
                env_key = ("TG_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()) if kind == "telegram" else ""
                cfg = {"name": name, "enabled": True, "mode": mode, "kind": kind, "token_env": env_key, "characters": [card_file],
                       "worlds": [], "model": DEFAULT_MODEL, "reply_language": "zh-CN", "user_label": "你"}
                if mode == "rpg":
                    cfg.update(extract_model="qwen3:14b", extract=True, rules=False, scenes=False, max_tokens=4096)
                self._bot_path(name).write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
                if kind == "local":
                    return {"ok": True, "bot": name, "restarted": False, "local": True,
                            "note": f"本地 bot「{name}」已创建，到「本地聊天」页即可使用。"}
                token = (n.get("token") or "").strip()
                if token:
                    self.set_token(env_key, token)
                if not token:
                    return {"ok": True, "bot": name, "restarted": False,
                            "note": f"已创建 bot「{name}」，但还没有 Telegram token。到 Bots 页粘贴 token 后重启引擎即可上线。"}
                self.engine.restart() if self.engine.alive else self.engine.start()
                return {"ok": True, "bot": name, "restarted": True, "note": f"bot「{name}」已创建并启动，正在连接 Telegram。"}

            name = target.get("bot")
            p = self._bot_path(name)
            if not p.exists():
                return {"ok": False, "error": f"bot 不存在: {name}"}
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            mode = raw.get("mode", "dialogue")
            chars = list(raw.get("characters") or [])
            if mode == "rpg":
                raw["characters"] = [card_file]
            elif card_file not in chars:
                chars.append(card_file)
                raw["characters"] = chars
            p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
            self.local.reload(name)
            running_bot = (self.engine.engine.bots.get(name) if (self.engine.alive and self.engine.engine) else None)
            if running_bot is not None and mode == "dialogue":
                hot_add_character(running_bot, card_file, self.data_dir)
                return {"ok": True, "bot": name, "restarted": False,
                        "note": f"已热加载到「{name}」，Telegram 里 /character 立刻可见。"}
            if self.engine.alive:
                self.engine.restart()
                return {"ok": True, "bot": name, "restarted": True, "note": f"已写入「{name}」并重启引擎。"}
            return {"ok": True, "bot": name, "restarted": False, "note": f"已写入「{name}」，启动引擎后生效。"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

    # ---- models page: catalog + one-click deploy ---------------------------------------------------------
    def models_page(self) -> dict:
        o = self.ollama()
        installed = {m["name"] for m in o.get("models", [])}
        ram = MODELS.machine_ram_gb()
        bots = load_bots(self.data_dir)
        return {"ollama": o, "ram_gb": ram, "tier": MODELS.tier_for(ram), "intro": MODELS.INTRO,
                "catalog": MODELS.catalog_for(installed, ram),
                "bots": [{"name": b.name, "mode": b.mode, "model": b.model, "extract_model": b.extract_model} for b in bots],
                "deploys": list(self._model_jobs.values())[-20:]}

    def model_deploy(self, name: str, bots: list, role: str = "chat") -> dict:
        """Pull `name` if missing, then set it as model / extract_model for the given bots and restart."""
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "模型名为空"}
        field = "extract_model" if role == "extract" else "model"
        job = {"name": name, "role": role, "bots": list(bots or []), "state": "pulling", "progress": "", "error": ""}
        self._model_jobs[name] = job

        def work():
            try:
                o = self.ollama()
                installed = {m["name"] for m in o.get("models", [])}
                if name not in installed:
                    def upd(pct, detail):
                        job["progress"] = f"{pct}%"
                        self._pull[name] = {"state": "pulling", "percent": pct, "detail": detail or f"{pct}%"}
                    ok, err = self._do_pull(name, upd)
                    if not ok:
                        job.update(state="failed", error=err or "拉取失败（模型名是否正确？）")
                        self._pull[name] = {"state": "failed", "percent": 0, "detail": err or "拉取失败"}
                        return
                    self._pull[name] = {"state": "done", "percent": 100, "detail": "完成"}
                    job["progress"] = "100%"
                job["state"] = "applying"
                changed = []
                for b in job["bots"]:
                    p = self._bot_path(b)
                    if not p.exists():
                        continue
                    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                    if raw.get(field) != name:
                        raw[field] = name
                        p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
                        changed.append(b)
                    self.local.reload(b)
                if changed and self.engine.alive:
                    self.engine.restart()
                job.update(state="done", progress="100%", applied=changed)
            except Exception as exc:  # noqa: BLE001
                job.update(state="failed", error=str(exc)[:160])

        threading.Thread(target=work, name="tavern-model-deploy", daemon=True).start()
        return {"ok": True}

    def model_remove(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "模型名为空"}
        url = self._ollama_url() + "/api/delete"
        body = json.dumps({"model": name}).encode()
        req = urllib.request.Request(url, data=body, method="DELETE",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:160]}

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
        _reveal(str(target))
        return {"ok": True}

    def open_url(self, url: str) -> dict:
        if url.startswith(("http://", "https://")):
            _reveal(url)
        return {"ok": True}


def main() -> None:
    data_dir = ensure_data_dir(default_data_dir())
    setup_logging(data_dir)
    api = Api(data_dir)
    webview.settings["ALLOW_DOWNLOADS"] = True
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
