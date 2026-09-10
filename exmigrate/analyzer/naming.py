"""Identifier normalisation for tables and columns."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def to_identifier(raw: str, fallback: str = "col") -> str:
    """Normalise ``raw`` into a snake_case identifier safe for SQL targets.

    Non-ASCII letters (e.g. Korean) are preserved since both SQLite and
    PostgreSQL accept them in quoted identifiers.
    """
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    text = _NON_WORD.sub("_", text).strip("_").lower()
    text = re.sub(r"_+", "_", text)
    if not text:
        return fallback
    if text[0].isdigit():
        text = f"_{text}"
    return text


def dedupe(names: Iterable[str]) -> list[str]:
    """Make ``names`` unique by suffixing ``_2``, ``_3`` ... to repeats."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen[name] = 1
            out.append(name)
            continue
        seen[name] += 1
        candidate = f"{name}_{seen[name]}"
        while candidate in seen:
            seen[name] += 1
            candidate = f"{name}_{seen[name]}"
        seen[candidate] = 1
        out.append(candidate)
    return out
