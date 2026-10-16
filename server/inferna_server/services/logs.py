"""Log snapshot helpers.

The control plane never dials a GPU host: worker logs arrive with the Sync the worker initiates
(see `docs/network-matrix.md` for why inbound access to GPU hosts is not part of the contract).
These helpers only shape what already arrived.
"""

from __future__ import annotations

DEFAULT_TAIL_LINES = 100


def tail_lines(text: str | None, lines: int = DEFAULT_TAIL_LINES) -> str | None:
    """Last `lines` lines of a log snapshot (None stays None — "no logs yet" is not "empty")."""
    if text is None:
        return None
    if lines <= 0:
        return ""
    kept = text.rstrip("\n").splitlines()[-lines:]
    return "\n".join(kept)
