"""SillyTavern-compatible World Info (lorebook) engine.

Reads two on-disk formats and normalises them into one `Entry` model:

* ST world JSON  (`worlds/*.json`):  {"entries": {"0": {key, keysecondary, content,
  constant, selective, selectiveLogic, order, position, depth, role, disable,
  probability, useProbability, scanDepth, matchWholeWords, caseSensitive,
  excludeRecursion, preventRecursion, group, ...}}}
* Character-card embedded book (V2/V3 `character_book`): entries list with
  {keys, secondary_keys, content, constant, selective, enabled, insertion_order,
  position: "before_char"|"after_char", extensions: {position, depth, role,
  probability, useProbability, selectiveLogic, scan_depth, match_whole_words,
  case_sensitive, exclude_recursion, prevent_recursion, group}}

Semantics follow SillyTavern (world-info.js) for the subset that matters:
  - constant entries always activate;
  - primary keys: case-insensitive substring unless caseSensitive /
    matchWholeWords; `/pattern/flags` keys are regexes;
  - secondary keys with selectiveLogic 0 AND_ANY, 1 NOT_ALL, 2 NOT_ANY, 3 AND_ALL;
  - scan text = the last `scan_depth` chat messages + the pending user message;
  - probability (0-100) gate, deterministic when an `rng` is injected;
  - optional recursion: activated content is re-scanned (excludeRecursion /
    preventRecursion honoured);
  - token budget: higher `order` gets budget first; inside one position the
    output is ordered by `order` ascending (highest ends closest to chat).

Positions (ST numeric): 0 before char defs, 1 after char defs, 2 Author's Note
top, 3 AN bottom, 4 at depth (with role 0 system / 1 user / 2 assistant),
5 example-messages top, 6 EM bottom.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ---- selective logic -------------------------------------------------------
AND_ANY, NOT_ALL, NOT_ANY, AND_ALL = 0, 1, 2, 3

# ---- positions -------------------------------------------------------------
POS_BEFORE_CHAR, POS_AFTER_CHAR, POS_AN_TOP, POS_AN_BOTTOM, POS_AT_DEPTH, POS_EM_TOP, POS_EM_BOTTOM = range(7)
_CARD_POS = {"before_char": POS_BEFORE_CHAR, "after_char": POS_AFTER_CHAR}
_ROLE_NAMES = {0: "system", 1: "user", 2: "assistant"}


@dataclass
class Entry:
    uid: str
    keys: list[str]
    secondary_keys: list[str]
    content: str
    constant: bool = False
    selective: bool = False
    selective_logic: int = AND_ANY
    order: int = 100
    position: int = POS_BEFORE_CHAR
    depth: int = 4
    role: int = 0
    enabled: bool = True
    probability: int = 100
    use_probability: bool = True
    scan_depth: int | None = None
    match_whole_words: bool | None = None
    case_sensitive: bool | None = None
    exclude_recursion: bool = False
    prevent_recursion: bool = False
    group: str = ""
    comment: str = ""
    source: str = ""          # file / card name, for debug

    @property
    def role_name(self) -> str:
        return _ROLE_NAMES.get(self.role, "system")


@dataclass
class Activation:
    entry: Entry
    reason: str               # "constant" | "key:<k>" | "recursion:<k>"


@dataclass
class WorldInfoResult:
    before_char: str = ""
    after_char: str = ""
    an_top: str = ""
    an_bottom: str = ""
    em_top: str = ""
    em_bottom: str = ""
    depth_entries: list[tuple[int, str, str]] = field(default_factory=list)  # (depth, role, content)
    activated: list[Activation] = field(default_factory=list)
    dropped_for_budget: list[Entry] = field(default_factory=list)
    tokens_used: int = 0


# ---- token estimate (no tokenizer dependency) --------------------------------
def estimate_tokens(text: str) -> int:
    """Rough, tokenizer-free estimate: CJK ≈ 1 token/char, other ≈ 1 token/4 chars."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "぀" <= ch <= "ヿ" or "㐀" <= ch <= "鿿"
              or "가" <= ch <= "힯" or "豈" <= ch <= "﫿")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


# ---- loading ----------------------------------------------------------------
def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(s).strip() for s in v if str(s).strip()]


def _bool(v, default=False) -> bool:
    return default if v is None else bool(v)


def _int(v, default: int) -> int:
    try:
        return default if v is None else int(v)
    except (TypeError, ValueError):
        return default


def entries_from_world_json(data: dict, source: str = "") -> list[Entry]:
    raw = data.get("entries") or {}
    items = list(raw.values()) if isinstance(raw, dict) else list(raw)
    out: list[Entry] = []
    for i, e in enumerate(items):
        if not isinstance(e, dict):
            continue
        out.append(Entry(
            uid=f"{source}#{e.get('uid', i)}",
            keys=_as_list(e.get("key")),
            secondary_keys=_as_list(e.get("keysecondary")),
            content=str(e.get("content") or ""),
            constant=_bool(e.get("constant")),
            selective=_bool(e.get("selective")),
            selective_logic=_int(e.get("selectiveLogic"), AND_ANY),
            order=_int(e.get("order"), 100),
            position=_int(e.get("position"), POS_BEFORE_CHAR),
            depth=_int(e.get("depth"), 4),
            role=_int(e.get("role"), 0),
            enabled=not _bool(e.get("disable")),
            probability=_int(e.get("probability"), 100),
            use_probability=_bool(e.get("useProbability"), True),
            scan_depth=e.get("scanDepth"),
            match_whole_words=e.get("matchWholeWords"),
            case_sensitive=e.get("caseSensitive"),
            exclude_recursion=_bool(e.get("excludeRecursion")),
            prevent_recursion=_bool(e.get("preventRecursion")),
            group=str(e.get("group") or ""),
            comment=str(e.get("comment") or ""),
            source=source,
        ))
    return out


def entries_from_character_book(book: dict | None, source: str = "card") -> list[Entry]:
    if not book:
        return []
    out: list[Entry] = []
    for i, e in enumerate(book.get("entries") or []):
        if not isinstance(e, dict):
            continue
        ext = e.get("extensions") or {}
        pos = ext.get("position")
        if pos is None:
            pos = _CARD_POS.get(str(e.get("position")), POS_BEFORE_CHAR)
        out.append(Entry(
            uid=f"{source}#{e.get('id', i)}",
            keys=_as_list(e.get("keys")),
            secondary_keys=_as_list(e.get("secondary_keys")),
            content=str(e.get("content") or ""),
            constant=_bool(e.get("constant")),
            selective=_bool(e.get("selective")),
            selective_logic=_int(ext.get("selectiveLogic"), AND_ANY),
            order=_int(e.get("insertion_order"), 100),
            position=_int(pos, POS_BEFORE_CHAR),
            depth=_int(ext.get("depth"), 4),
            role=_int(ext.get("role"), 0),
            enabled=_bool(e.get("enabled"), True),
            probability=_int(ext.get("probability"), 100),
            use_probability=_bool(ext.get("useProbability"), True),
            scan_depth=ext.get("scan_depth"),
            match_whole_words=ext.get("match_whole_words"),
            case_sensitive=ext.get("case_sensitive") if ext.get("case_sensitive") is not None else e.get("case_sensitive"),
            exclude_recursion=_bool(ext.get("exclude_recursion")),
            prevent_recursion=_bool(ext.get("prevent_recursion")),
            group=str(ext.get("group") or ""),
            comment=str(e.get("comment") or e.get("name") or ""),
            source=source,
        ))
    return out


def load_world_file(path: str | Path) -> list[Entry]:
    p = Path(path)
    with p.open(encoding="utf-8") as f:
        return entries_from_world_json(json.load(f), source=p.stem)


# ---- matching ----------------------------------------------------------------
_REGEX_KEY = re.compile(r"^/(.+)/([a-z]*)$", re.S)


def _key_matches(key: str, text: str, text_lower: str, *, case_sensitive: bool, whole_words: bool) -> bool:
    if not key:
        return False
    m = _REGEX_KEY.match(key)
    if m:
        flags = 0
        if "i" in m.group(2):
            flags |= re.I
        if "s" in m.group(2):
            flags |= re.S
        if "m" in m.group(2):
            flags |= re.M
        try:
            return re.search(m.group(1), text, flags) is not None
        except re.error:
            return False
    hay = text if case_sensitive else text_lower
    needle = key if case_sensitive else key.lower()
    if whole_words and re.fullmatch(r"[\w\s'\-]+", needle):
        # whole-word only meaningful for alphabetic keys; CJK falls back to substring
        return re.search(r"(?<![\w])" + re.escape(needle) + r"(?![\w])", hay) is not None
    return needle in hay


class WorldInfo:
    """A set of entries (from any number of books) plus global scan settings."""

    def __init__(self, entries: Iterable[Entry] = (), *, scan_depth: int = 2,
                 budget_tokens: int = 2048, recursive: bool = False,
                 case_sensitive: bool = False, match_whole_words: bool = False,
                 max_recursion_steps: int = 3, rng: random.Random | None = None):
        self.entries: list[Entry] = [e for e in entries]
        self.scan_depth = scan_depth
        self.budget_tokens = budget_tokens
        self.recursive = recursive
        self.case_sensitive = case_sensitive
        self.match_whole_words = match_whole_words
        self.max_recursion_steps = max_recursion_steps
        self.rng = rng or random.Random()

    # -- composition --------------------------------------------------------
    def add(self, entries: Iterable[Entry]) -> "WorldInfo":
        self.entries.extend(entries)
        return self

    @classmethod
    def from_sources(cls, *, card_book: dict | None = None, world_files: Iterable[str | Path] = (),
                     card_name: str = "card", **kw) -> "WorldInfo":
        wi = cls(**kw)
        wi.add(entries_from_character_book(card_book, source=card_name))
        for f in world_files:
            wi.add(load_world_file(f))
        return wi

    # -- activation ---------------------------------------------------------
    def _scan_text(self, history: list[dict], user_message: str, depth: int) -> str:
        msgs = [m.get("content", "") for m in history if m.get("content")]
        tail = msgs[-depth:] if depth > 0 else []
        return "\n".join(tail + [user_message or ""])

    def _entry_matches(self, e: Entry, history: list[dict], user_message: str) -> str | None:
        depth = e.scan_depth if isinstance(e.scan_depth, int) and e.scan_depth > 0 else self.scan_depth
        text = self._scan_text(history, user_message, depth)
        return self._match_text(e, text)

    def _match_text(self, e: Entry, text: str) -> str | None:
        cs = self.case_sensitive if e.case_sensitive is None else bool(e.case_sensitive)
        ww = self.match_whole_words if e.match_whole_words is None else bool(e.match_whole_words)
        low = text.lower()
        hit = next((k for k in e.keys if _key_matches(k, text, low, case_sensitive=cs, whole_words=ww)), None)
        if hit is None:
            return None
        if e.selective and e.secondary_keys:
            sec = [bool(_key_matches(k, text, low, case_sensitive=cs, whole_words=ww)) for k in e.secondary_keys]
            ok = {AND_ANY: any(sec), AND_ALL: all(sec), NOT_ANY: not any(sec), NOT_ALL: not all(sec)}.get(e.selective_logic, any(sec))
            if not ok:
                return None
        return hit

    def _passes_probability(self, e: Entry) -> bool:
        if not e.use_probability or e.probability >= 100:
            return True
        return self.rng.randint(1, 100) <= max(0, e.probability)

    def activate(self, history: list[dict], user_message: str = "") -> WorldInfoResult:
        res = WorldInfoResult()
        active: dict[str, Activation] = {}

        for e in self.entries:
            if not e.enabled or not e.content.strip():
                continue
            if e.constant:
                active[e.uid] = Activation(e, "constant")
                continue
            if not e.keys:
                continue
            hit = self._entry_matches(e, history, user_message)
            if hit is not None and self._passes_probability(e):
                active[e.uid] = Activation(e, f"key:{hit}")

        # recursion: scan newly activated content for further triggers
        if self.recursive:
            frontier = [a.entry for a in active.values() if not a.entry.prevent_recursion]
            steps = 0
            while frontier and steps < self.max_recursion_steps:
                steps += 1
                text = "\n".join(e.content for e in frontier)
                new: list[Entry] = []
                for e in self.entries:
                    if e.uid in active or not e.enabled or e.constant or e.exclude_recursion or not e.keys:
                        continue
                    hit = self._match_text(e, text)
                    if hit is not None and self._passes_probability(e):
                        active[e.uid] = Activation(e, f"recursion:{hit}")
                        if not e.prevent_recursion:
                            new.append(e)
                frontier = new

        # budget: higher order first
        kept: list[Entry] = []
        used = 0
        for a in sorted(active.values(), key=lambda a: -a.entry.order):
            t = estimate_tokens(a.entry.content)
            if used + t > self.budget_tokens and kept:
                res.dropped_for_budget.append(a.entry)
                continue
            kept.append(a.entry)
            used += t
        res.tokens_used = used
        res.activated = [a for a in active.values() if a.entry in kept]

        # render per position, order ascending inside each
        def _join(pos: int) -> str:
            return "\n".join(e.content.strip() for e in sorted(kept, key=lambda e: e.order) if e.position == pos)

        res.before_char = _join(POS_BEFORE_CHAR)
        res.after_char = _join(POS_AFTER_CHAR)
        res.an_top = _join(POS_AN_TOP)
        res.an_bottom = _join(POS_AN_BOTTOM)
        res.em_top = _join(POS_EM_TOP)
        res.em_bottom = _join(POS_EM_BOTTOM)
        res.depth_entries = [(e.depth, e.role_name, e.content.strip())
                             for e in sorted(kept, key=lambda e: e.order) if e.position == POS_AT_DEPTH]
        return res

    # -- debug ----------------------------------------------------------------
    def summary(self) -> str:
        by_src: dict[str, int] = {}
        for e in self.entries:
            by_src[e.source] = by_src.get(e.source, 0) + 1
        const = sum(1 for e in self.entries if e.constant)
        return f"{len(self.entries)} entries from {len(by_src)} source(s) {by_src}; constant={const}"
