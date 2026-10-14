"""Request traces: record what a run sent, and replay it later (autoscaling, parity reruns).

Format: one JSON object per line — ``{"ts": <epoch seconds>, "method": ..., "path": ...,
"body": {...}}``. Inter-arrival times are preserved on replay so a recorded load shape can be
reproduced against another build.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TraceEntry:
    ts: float
    method: str
    path: str
    body: dict[str, Any]


class TraceWriter:
    """Append-only JSONL writer (also usable as a context manager)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")

    def write(
        self, method: str, path: str, body: dict[str, Any], *, ts: float | None = None
    ) -> None:
        entry = {
            "ts": time.time() if ts is None else ts,
            "method": method,
            "path": path,
            "body": body,
        }
        self._handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_trace(path: str | Path) -> list[TraceEntry]:
    entries: list[TraceEntry] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        entries.append(
            TraceEntry(
                ts=float(payload["ts"]),
                method=str(payload.get("method", "POST")),
                path=str(payload.get("path", "/v1/chat/completions")),
                body=payload.get("body") or {},
            )
        )
    return entries


def replay_schedule(entries: list[TraceEntry]) -> Iterator[float]:
    """Yield the delay (seconds) to wait before each entry, preserving inter-arrival times."""
    previous: float | None = None
    for entry in entries:
        if previous is None:
            yield 0.0
        else:
            yield max(0.0, entry.ts - previous)
        previous = entry.ts
