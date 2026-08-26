"""Shared naming. Kept in one place so the two books agree on what a sport is
called — otherwise "Table Tennis" and "tabletennis" become two studies."""
from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slug(name: str) -> str:
    """Human sport name -> stable key. "Table Tennis" -> "table_tennis"."""
    return _NON_ALNUM.sub("_", (name or "").strip().lower()).strip("_") or "unknown"
