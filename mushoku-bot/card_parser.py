#!/usr/bin/env python3
"""
card_parser.py — extract character card content from SillyTavern PNG.

SillyTavern stores the character card (V2/V3 spec) as a base64-encoded
JSON blob inside a PNG tEXt chunk with keyword `chara`. This module reads
that chunk and exposes the parsed fields needed at runtime:
  - description (card's main scenario/persona/format hints)
  - personality (personality field)
  - scenario (scenario field, often a prompt to the model)
  - system_prompt (post_history_instructions or char-level system_prompt)
  - first_mes (greeting / opening line)
  - lorebook entries (character_book field; aggregated hints)
  - tags

If the PNG isn't ST-format (no `chara` tEXt), the module returns None and
the L1 fallback path is skipped.
"""
from __future__ import annotations
import base64
import json
import struct
import zlib
from typing import Any


def _read_png_chunks(data: bytes) -> list[tuple[str, bytes]]:
    """Yield (chunk_type, chunk_data) pairs from a PNG byte stream."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    out = []
    i = 8
    while i < len(data):
        if i + 8 > len(data):
            break
        length = struct.unpack(">I", data[i : i + 4])[0]
        ctype = data[i + 4 : i + 8].decode("ascii", errors="replace")
        cdata = data[i + 8 : i + 8 + length]
        out.append((ctype, cdata))
        i += 8 + length + 4  # length + type + data + crc
        if ctype == "IEND":
            break
    return out


def _decode_chara_text(raw: bytes) -> str | None:
    """PNG chara tEXt is `keyword \x00 text`. Decode base64 if present."""
    # Some cards store raw JSON, some store base64-encoded JSON. Detect by trying.
    try:
        txt = raw.decode("utf-8", errors="ignore").strip("\x00").strip()
    except Exception:
        return None
    if not txt:
        return None
    if txt.startswith("{"):
        return txt
    try:
        decoded = base64.b64decode(txt + "=" * ((4 - len(txt) % 4) % 4))
        return decoded.decode("utf-8", errors="ignore")
    except Exception:
        return None


def parse_card_png(png_path: str) -> dict[str, Any] | None:
    """Parse a SillyTavern PNG character card into a runtime-friendly dict.

    Returns None if the PNG has no `chara` tEXt (not an ST card).
    """
    try:
        with open(png_path, "rb") as f:
            data = f.read()
    except Exception:
        return None

    chunks = _read_png_chunks(data)
    chara_json: dict | None = None
    for ctype, cdata in chunks:
        if ctype != "tEXt":
            continue
        nul = cdata.find(b"\x00")
        if nul < 0:
            continue
        kw = cdata[:nul].decode("latin-1", errors="replace").lower()
        if kw == "chara":
            txt = _decode_chara_text(cdata[nul + 1 :])
            if txt:
                try:
                    chara_json = json.loads(txt)
                    break
                except Exception:
                    continue

    if not isinstance(chara_json, dict):
        return None

    # V2 nests content under "data"; V3 may be flat (rare for chara chunks).
    data_block = chara_json.get("data", chara_json)

    description = (data_block.get("description") or "").strip()
    personality = (data_block.get("personality") or "").strip()
    scenario = (data_block.get("scenario") or "").strip()
    system_prompt = (data_block.get("system_prompt") or "").strip()
    post_history = (data_block.get("post_history_instructions") or "").strip()
    first_mes = (data_block.get("first_mes") or "").strip()
    if not system_prompt and post_history:
        system_prompt = post_history

    char_book = data_block.get("character_book", {}) or {}
    lore_entries: list[dict] = []
    if isinstance(char_book, dict):
        for e in char_book.get("entries", []) or []:
            if not isinstance(e, dict):
                continue
            keys = e.get("keys", []) or e.get("key", [])
            content = (e.get("content") or "").strip()
            if not content:
                continue
            keys = keys if isinstance(keys, list) else [keys]
            lore_entries.append({
                "keys": [str(k) for k in keys],
                "content": content,
                "enabled": e.get("enabled", True),
            })

    tags = data_block.get("tags", []) or []
    if isinstance(tags, list):
        tags = [str(t) for t in tags]
    else:
        tags = []

    return {
        "name": (data_block.get("name") or "").strip(),
        "description": description,
        "personality": personality,
        "scenario": scenario,
        "system_prompt": system_prompt,
        "first_mes": first_mes,
        "tags": tags,
        "lorebook": lore_entries,
        "lorebook_count": len(lore_entries),
    }


def build_system_prompt_block(card: dict[str, Any], base_override: str) -> str:
    """Compose a system prompt combining:
      1. base_override (cult-RP override; highest priority narrative guard)
      2. card.description (scenario setting + format hints)
      3. card.personality (tone instructions)
      4. card.scenario (optional extra setup)
      5. card.system_prompt (card-level system prompt, if any)
      6. lorebook entries (as a numbered list with keys tags, max ~6 to bound size)

    Returns a single string suitable as a `system` role message.
    """
    parts: list[str] = []
    parts.append(base_override.strip())

    if card.get("description"):
        parts.append("【角色卡 — Description】\n" + card["description"])
    if card.get("personality"):
        parts.append("【角色卡 — Personality】\n" + card["personality"])
    if card.get("scenario"):
        parts.append("【角色卡 — Scenario】\n" + card["scenario"])
    if card.get("system_prompt"):
        parts.append("【角色卡 — System Prompt】\n" + card["system_prompt"])
    lore = card.get("lorebook") or []
    if lore:
        # Bound to first 6 entries to keep prompt size manageable.
        items: list[str] = []
        for e in lore[:6]:
            keys = ", ".join(e.get("keys") or []) or "(no keys)"
            items.append(f"  - [{keys}] {e['content']}")
        parts.append("【Character Book (前 6 条)】\n" + "\n".join(items))

    return "\n\n".join(parts)
