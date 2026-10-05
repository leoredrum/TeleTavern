#!/usr/bin/env python3
"""World Info engine checks against Leo's three real books + synthetic edge cases.

Run:  cd ~/Documents/telegramtavern && penelope/venv/bin/python tests/test_worldinfo.py
"""
from __future__ import annotations

import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "penelope"))   # character_card.py lives there too

from tavern.worldinfo import (  # noqa: E402
    AND_ALL, NOT_ANY, POS_AT_DEPTH, Entry, WorldInfo, entries_from_character_book,
    load_world_file,
)

DM_DATA = "/Users/leo/Documents/SillyTavern/dm-data/default-user"
WORLDS = {
    "mushoku": f"{DM_DATA}/worlds/Mushoku Tensei World Reference.json",
    "blessed": f"{DM_DATA}/worlds/[Blessed Are The Fruitful] - Complete Lorebook.json",
    "eldoria": f"{DM_DATA}/worlds/Eldoria.json",
}
DM_CARD = f"{DM_DATA}/characters/aicc-2025-07-24_DungeonMaster12.png"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def real_books() -> None:
    print("[real books]")
    for name, path in WORLDS.items():
        if not os.path.exists(path):
            print(f"  skip {name}: missing")
            continue
        ents = load_world_file(path)
        wi = WorldInfo(ents, scan_depth=2, budget_tokens=4096)
        const = [e for e in ents if e.constant]
        res_empty = wi.activate([], "")
        check(len(res_empty.activated) == len(const),
              f"{name}: empty scan activates only constants ({len(const)})")
        # pick a keyed entry and trigger it with its first key
        keyed = next((e for e in ents if e.keys and not e.constant and e.enabled), None)
        if keyed:
            res = wi.activate([{"role": "user", "content": f"我们聊聊 {keyed.keys[0]} 吧"}], "继续")
            check(any(a.entry.uid == keyed.uid for a in res.activated),
                  f"{name}: key '{keyed.keys[0][:20]}' in history triggers its entry")
            check(keyed.content.strip() in (res.before_char + res.after_char + res.an_top + res.an_bottom
                                            + res.em_top + res.em_bottom + "\n".join(c for _, _, c in res.depth_entries)),
                  f"{name}: triggered content rendered at its position")
        print(f"       {wi.summary()}")


def dm_card_book() -> None:
    print("[DM card embedded book]")
    if not os.path.exists(DM_CARD):
        print("  skip: card missing")
        return
    from character_card import load_character
    card = load_character(DM_CARD)
    ents = entries_from_character_book(card.character_book, source="DungeonMaster12")
    check(len(ents) == 68, f"68 entries parsed (got {len(ents)})")
    wi = WorldInfo(ents, scan_depth=2, budget_tokens=4096)
    e0 = ents[0]
    res = wi.activate([], e0.keys[0])
    check(any(a.entry.uid == e0.uid for a in res.activated), f"pending user message triggers '{e0.keys[0]}'")
    res_none = wi.activate([{"role": "assistant", "content": "你走进酒馆，点了一杯麦酒。"}], "我四处看看")
    check(len(res_none.activated) == sum(1 for e in ents if e.constant),
          "unrelated tavern text activates nothing but constants")
    # scan depth: a key 3 messages back must NOT trigger with scan_depth=2
    hist = [{"role": "user", "content": e0.keys[0]},
            {"role": "assistant", "content": "……"},
            {"role": "user", "content": "……"}]
    check(not any(a.entry.uid == e0.uid for a in wi.activate(hist, "继续").activated),
          "key older than scan_depth does not trigger")


def synthetic() -> None:
    print("[synthetic semantics]")
    ents = [
        Entry("c", [], [], "CONST", constant=True, order=10),
        Entry("a", ["dragon"], [], "DRAGON", order=50),
        Entry("b", ["dragon"], ["red"], "RED-DRAGON", selective=True, selective_logic=AND_ALL, order=60),
        Entry("n", ["dragon"], ["dead"], "LIVE-DRAGON", selective=True, selective_logic=NOT_ANY, order=70),
        Entry("r", ["/gob+lin/i"], [], "GOBLIN", order=20),
        Entry("d", ["cave"], [], "CAVE-DEPTH", position=POS_AT_DEPTH, depth=2, role=1, order=30),
        Entry("rec", ["ember"], [], "EMBER-RECURSED", order=40),
        Entry("src", ["lair"], [], "the lair glows with ember", order=45),
        Entry("p", ["coin"], [], "COIN-50", probability=50, order=5),
        Entry("big", ["huge"], [], "X" * 4000, order=1),
        Entry("off", ["dragon"], [], "DISABLED", enabled=False),
    ]
    wi = WorldInfo(ents, scan_depth=2, budget_tokens=200, rng=random.Random(7))
    r = wi.activate([{"role": "user", "content": "A Dragon appears"}], "it is dead")
    got = {a.entry.uid for a in r.activated}
    check("c" in got, "constant always on")
    check("a" in got, "case-insensitive primary key")
    check("b" not in got, "AND_ALL secondary missing → not activated")
    check("n" not in got, "NOT_ANY secondary present → not activated")
    check("off" not in got, "disabled entry ignored")
    r = wi.activate([], "a red dragon")
    check("b" in {a.entry.uid for a in r.activated}, "AND_ALL satisfied")
    check("n" in {a.entry.uid for a in r.activated}, "NOT_ANY satisfied when secondary absent")
    r = wi.activate([], "a gobbbbblin!")
    check("r" in {a.entry.uid for a in r.activated}, "regex key /gob+lin/i")
    r = wi.activate([], "into the cave")
    check(r.depth_entries == [(2, "user", "CAVE-DEPTH")], "@depth entry rendered with depth+role")
    r = WorldInfo(ents, recursive=True, budget_tokens=500).activate([], "the lair")
    check("rec" in {a.entry.uid for a in r.activated}, "recursion: 'ember' inside activated content triggers")
    r = WorldInfo(ents, recursive=False, budget_tokens=500).activate([], "the lair")
    check("rec" not in {a.entry.uid for a in r.activated}, "recursion off → no chained activation")
    hits = sum(1 for _ in range(200) if "p" in {a.entry.uid for a in wi.activate([], "a coin").activated})
    check(60 <= hits <= 140, f"probability 50% ≈ half of 200 runs (got {hits})")
    r = wi.activate([], "huge dragon")
    check(any(e.uid == "big" for e in r.dropped_for_budget) and "a" in {a.entry.uid for a in r.activated},
          "budget: low-order oversized entry dropped, higher-order kept")
    r = wi.activate([], "red dragon")
    check(r.before_char.index("DRAGON") < r.before_char.index("RED-DRAGON"),
          "render order ascending by `order` within a position")


if __name__ == "__main__":
    real_books()
    dm_card_book()
    synthetic()
    print("\n" + ("PASS" if not fails else f"FAIL ({len(fails)}):\n  " + "\n  ".join(fails)))
    sys.exit(1 if fails else 0)
