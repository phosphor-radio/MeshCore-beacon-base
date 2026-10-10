"""Beacon and repeater names: cleaning untrusted text and how names are shown.

A beacon name arrives in a repeater's announcement, which anyone holding the channel key can forge or replay, and it ends up
on a terminal (and later a web page). So it is cleaned on the way in. Names are for display only; nothing is decided by
one.
"""

from __future__ import annotations

import unicodedata

MAX_NAME_BYTES = 32
MIN_REF_DIGITS = 6  # shortest key prefix accepted when naming a beacon or repeater on the command line


def _truncate_utf8(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore")  # 'ignore' drops a character cut in half


def clean_name(text: str, limit: int = MAX_NAME_BYTES) -> str | None:
    """Make text safe to show: control, format and separator characters (including escape sequences and bidirectional
    overrides) become spaces, runs of whitespace collapse, and the result is cut to `limit` UTF-8 bytes. Returns None if
    nothing is left."""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat in ("Zl", "Zp"):
            out.append(" ")
        elif cat == "Zs":
            out.append(" ")
        else:
            out.append(ch)
    cleaned = " ".join("".join(out).split())
    cleaned = _truncate_utf8(cleaned, limit).strip()
    return cleaned or None


def sanitize_name(raw: bytes, limit: int = MAX_NAME_BYTES) -> str | None:
    """Clean a name announced over the air: invalid UTF-8 is replaced, then as clean_name."""
    return clean_name(raw.decode("utf-8", "replace"), limit)


def prefix_label(prefix: bytes) -> str:
    """The first 6 hex digits, the form used in messages and by the beacons' default names."""
    return bytes(prefix).hex()[:MIN_REF_DIGITS]


def label(name: str | None, prefix: bytes) -> str:
    """How a beacon or repeater is named in one-line messages: `name (f5b165)`, or the first 12 digits without a name."""
    if name:
        return f"{name} ({prefix_label(prefix)})"
    return bytes(prefix).hex()[:12]
