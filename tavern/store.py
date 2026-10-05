"""aicharactercards.com client — browse the catalog and download cards straight
into characters/.

Public REST API (no browser challenge):
  GET /api/cards?limit&skip&search&language&tags=<id,id>&excludeTags&nsfw&orderBy
  GET /api/cards/trending?period=24h|7d|30d&limit
  GET /api/cards/most-downloaded?range=week|month|all&limit&skip
  GET /api/cards/metadata/tags · /api/cards/metadata/languages
  GET /api/cards/{id} · GET /api/cards/{id}/download?format=st   (rate limited → 429)
Auth: the website keeps a JWT in localStorage["token"]; we send it as
`Authorization: Bearer …` (needed for NSFW / account features).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("tavern.store")

API = "https://api.aicharactercards.com/api"
SITE = "https://aicharactercards.com"
CDN = "https://cdn.aicharactercards.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) TelegramTavern/3.0"


class StoreError(RuntimeError):
    pass


def image_url(path: str | None) -> str:
    if not path:
        return ""
    if path.startswith("http"):
        return path
    p = path[len("/uploads"):] if path.startswith("/uploads/") else path
    return CDN + (p if p.startswith("/") else "/" + p)


def safe_filename(title: str, card_id: int) -> str:
    base = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", title or "").strip().strip(".")
    base = re.sub(r"\s+", " ", base)[:80] or f"card-{card_id}"
    return f"{base} [aicc-{card_id}].png"


class StoreClient:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.auth_path = self.data_dir / "store_auth.json"
        self.token: str = ""
        self.user: dict = {}
        self._load_auth()

    # ---- auth ------------------------------------------------------------------------
    def _load_auth(self) -> None:
        try:
            d = json.loads(self.auth_path.read_text(encoding="utf-8"))
            self.token = d.get("token", "") or ""
            self.user = d.get("user", {}) or {}
        except Exception:  # noqa: BLE001
            self.token, self.user = "", {}

    def save_auth(self, token: str, user: dict | None = None) -> None:
        self.token = token or ""
        self.user = user or {}
        self.auth_path.write_text(json.dumps({"token": self.token, "user": self.user}, ensure_ascii=False),
                                  encoding="utf-8")
        os.chmod(self.auth_path, 0o600)

    def logout(self) -> None:
        self.token, self.user = "", {}
        if self.auth_path.exists():
            self.auth_path.unlink()

    def me(self) -> dict | None:
        if not self.token:
            return None
        try:
            d = self._get("/auth/me")
            u = d.get("user") or d.get("data") or d
            if isinstance(u, dict) and u:
                self.user = {k: u.get(k) for k in ("id", "username", "email", "role", "ageVerified", "isAgeVerified") if k in u}
                self.save_auth(self.token, self.user)
            return u
        except StoreError as exc:
            if "401" in str(exc) or "403" in str(exc):
                self.logout()
            return None

    # ---- http ------------------------------------------------------------------------
    def _headers(self) -> dict:
        h = {"User-Agent": UA, "Accept": "application/json, image/png, */*", "Origin": SITE, "Referer": SITE + "/"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, path: str, params: dict | None = None, *, raw: bool = False, timeout: int = 30):
        q = {k: v for k, v in (params or {}).items() if v not in (None, "", [], False)}
        url = API + path + (("?" + urllib.parse.urlencode(q, doseq=True)) if q else "")
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8", "ignore")).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            raise StoreError(f"HTTP {e.code} {detail}".strip()) from None
        except Exception as exc:  # noqa: BLE001
            raise StoreError(str(exc)[:160]) from None
        if raw:
            return body, ctype
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            raise StoreError("invalid JSON from API") from None

    # ---- catalog ----------------------------------------------------------------------------
    @staticmethod
    def _norm(c: dict) -> dict:
        return {"id": c.get("id"), "title": c.get("titleEn") or c.get("title") or f"#{c.get('id')}",
                "title_orig": c.get("title"), "author": c.get("author") or "", "language": c.get("language") or "",
                "nsfw": bool(c.get("isNsfw")), "downloads": c.get("downloadCount") or 0,
                "rating": c.get("ratingAvg"), "ratings": c.get("ratingCount") or 0, "tokens": c.get("tokenCount") or 0,
                "ai_score": c.get("aiScore"), "animated": bool(c.get("isAnimated")),
                "excerpt": (c.get("excerptEn") or c.get("excerpt") or c.get("description") or "")[:220],
                "image": image_url(c.get("imageUrl")), "tags": [t.get("name") for t in (c.get("tags") or []) if isinstance(t, dict)],
                "created": (c.get("createdAt") or "")[:10], "url": f"{SITE}/cards/{c.get('id')}"}

    def browse(self, *, source: str = "recent", search: str = "", language: str = "", tags: list[str] | None = None,
               nsfw: str = "", sort: str = "", limit: int = 24, skip: int = 0, period: str = "7d",
               range_: str = "month") -> dict:
        if source == "trending":
            d = self._get("/cards/trending", {"period": period, "limit": limit, "tags": ",".join(tags or [])})
            items = d.get("data") or []
            total = d.get("total") or len(items)
        elif source == "downloads":
            d = self._get("/cards/most-downloaded", {"range": range_, "limit": limit, "skip": skip})
            items = d.get("data") or []
            total = (d.get("pagination") or {}).get("total", len(items))
        else:
            d = self._get("/cards", {"limit": limit, "skip": skip, "search": search, "language": language,
                                     "tags": ",".join(str(t) for t in (tags or [])), "nsfw": nsfw,
                                     "orderBy": sort})   # orderBy: downloadCount | ratingAvg | aiScore | createdAt
            items = d.get("data") or []
            total = (d.get("pagination") or {}).get("total", len(items))
        return {"items": [self._norm(c) for c in items if isinstance(c, dict)], "total": total,
                "skip": skip, "limit": limit}

    def tags(self) -> list[dict]:
        d = self._get("/cards/metadata/tags")
        return [{"id": t.get("id"), "name": t.get("name"), "slug": t.get("slug"), "rating": t.get("rating")}
                for t in d.get("data") or []]

    def languages(self) -> list[dict]:
        d = self._get("/cards/metadata/languages")
        return [{"code": x.get("code"), "name": x.get("name")} for x in d.get("data") or []]

    def detail(self, card_id: int) -> dict:
        return self._norm(self._get(f"/cards/{int(card_id)}"))

    # ---- download ------------------------------------------------------------------------------
    def download(self, card_id: int, dest_dir: Path, *, title: str = "", fmt: str = "st",
                 retries: int = 4) -> Path:
        """Download one card PNG (SillyTavern format) into dest_dir, backing off on 429."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        delay = 3.0
        for attempt in range(retries + 1):
            try:
                body, ctype = self._get(f"/cards/{int(card_id)}/download", {"format": fmt}, raw=True, timeout=120)
            except StoreError as exc:
                if "429" in str(exc) and attempt < retries:
                    log.info("store: 429 on %s, retry in %.0fs", card_id, delay)
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise
            if not body[:8] == b"\x89PNG\r\n\x1a\n":
                raise StoreError(f"not a PNG ({ctype}, {len(body)} bytes)")
            name = safe_filename(title, int(card_id))
            path = dest_dir / name
            path.write_bytes(body)
            return path
        raise StoreError("rate limited")
