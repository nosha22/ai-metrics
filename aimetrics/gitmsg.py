"""Parsing of trailers and co-authors in commit messages."""
from __future__ import annotations

import re

_TRAILER = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s*(.+?)\s*$")


def parse_trailers(message: str) -> list[str]:
    """Returns the trailers in the message's last paragraph, as "Key: value".

    Follows git's convention: trailers live in the last block separated by a blank line,
    and each line has the form "Key: value". Lines in that block that are not trailers
    (e.g. "🤖 Generated with Claude Code") are skipped instead of invalidating the block,
    because AI tools often mix them in.
    """
    if not message:
        return []
    blocks = re.split(r"\n\s*\n", message.strip())
    if len(blocks) < 2:
        return []
    out = []
    for line in blocks[-1].splitlines():
        m = _TRAILER.match(line.strip())
        if m and not m.group(2).startswith("//"):
            out.append(f"{m.group(1)}: {m.group(2)}")
    return out


def co_authors(trailers: list[str]) -> list[str]:
    return [t.split(":", 1)[1].strip() for t in trailers if t.lower().startswith("co-authored-by:")]
