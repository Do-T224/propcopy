"""Parse a routing tag from the source trade's MT5 comment."""

from __future__ import annotations

import re

_TAG_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def parse_comment_tag(comment: str) -> str:
    """``"MYTAG:LONG"`` -> ``"MYTAG"``. Returns ``""`` when the comment has no
    ``TAG:`` prefix (manual trades, free-text comments).

    Trades that share a tag and side are treated as one multi-leg entry by the
    ``merge`` small-leg policy; untagged trades are always standalone entries.
    """
    if not comment or ":" not in comment:
        return ""
    head = comment.split(":", 1)[0].strip()
    return head if _TAG_RE.match(head) else ""
