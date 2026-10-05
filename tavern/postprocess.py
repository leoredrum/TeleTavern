"""Output post-processing: romanization/English → Chinese replacement tables.

Ported from connector/saengmyeong-bot/ollama_fallback.translate_to_chinese:
longer sources are replaced first so "High Priestess" wins over "Priestess".
"""
from __future__ import annotations


def apply_translation(text: str, table) -> str:
    if not text or not table:
        return text
    pairs = []
    for row in table:
        if isinstance(row, (list, tuple)) and len(row) == 2 and row[0]:
            pairs.append((str(row[0]), str(row[1])))
        elif isinstance(row, dict) and row.get("from"):
            pairs.append((str(row["from"]), str(row.get("to", ""))))
    for src, dst in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        text = text.replace(src, dst)
    return text
