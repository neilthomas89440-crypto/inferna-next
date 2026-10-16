"""Prefix-cache awareness for instance selection.

Engines (vLLM, SGLang) cache the KV blocks of a prompt prefix automatically, so a request that
repeats a prefix already held by a replica is served faster there. The gateway knows exactly which
replica served which prefix — it routed the request — so it keeps that map and prefers a replica
whose resident prefix overlaps the incoming one, falling back to least-loaded/LRU when nothing
matches.

Residency is an inference, not engine introspection: it is what *this* gateway has recently sent,
bounded by time and size. That is enough for agent loops (the same long prefix resent dozens of
times) and deliberately conservative — a wrong guess costs one cold prefill, never correctness.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import deque

# Blocks are hashed in whole units so two prompts that share a prefix share hashes. ~4 chars per
# token for English; the exact size only has to be stable, not exact.
PREFIX_BLOCK_TOKENS = 128
CHARS_PER_TOKEN = 4
PREFIX_BLOCK_CHARS = PREFIX_BLOCK_TOKENS * CHARS_PER_TOKEN
# A prefix is treated as resident for this long: engines evict on their own schedule and the
# safest assumption is "probably gone" rather than "definitely there".
PREFIX_TTL_SECONDS = 600.0
# Per-instance ring of recently seen prefixes; an agent loop resends one prefix, a chat user
# rarely repeats, so a small ring captures the traffic that matters.
MAX_PREFIXES_PER_INSTANCE = 64


def prompt_text(messages: list[dict] | None) -> str:
    """Flatten chat messages into the text the engine tokenizes (roles included)."""
    if not messages:
        return ""
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", ""))
        content = message.get("content")
        if isinstance(content, list):  # vision-style content parts
            content = " ".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        parts.append(f"{role}:{content if isinstance(content, str) else ''}")
    return "\n".join(parts)


def blocks(text: str, *, block_chars: int = PREFIX_BLOCK_CHARS) -> list[str]:
    """Rolling-block digests of `text` (block i covers characters `[i*size, (i+1)*size)`).

    Chained like the usual prefix-hash scheme: block `i` hashes the previous digest plus its own
    slice, so a change anywhere shifts every later digest — which is what makes "same prefix"
    mean the same blocks.
    """
    if not text:
        return []
    digests: list[str] = []
    previous = ""
    for start in range(0, len(text), block_chars):
        chunk = text[start : start + block_chars]
        digest = hashlib.sha256(f"{previous}|{chunk}".encode()).hexdigest()[:16]
        digests.append(digest)
        previous = digest
    return digests


class PrefixCacheIndex:
    """Which instance recently served which prompt-prefix blocks (bounded, time-limited)."""

    def __init__(
        self,
        *,
        ttl_seconds: float = PREFIX_TTL_SECONDS,
        max_per_instance: int = MAX_PREFIXES_PER_INSTANCE,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_per_instance = max_per_instance
        self._seen: dict[uuid.UUID | str, deque[tuple[float, frozenset[str]]]] = {}
        self.matches = 0
        self.lookups = 0

    def record(self, instance_id: uuid.UUID | str, digests: list[str]) -> None:
        """Remember that `instance_id` served a prompt whose blocks are `digests`."""
        if not digests:
            return
        ring = self._seen.setdefault(instance_id, deque(maxlen=self.max_per_instance))
        ring.append((time.monotonic(), frozenset(digests)))

    def overlap(self, instance_id: uuid.UUID | str, digests: list[str]) -> int:
        """Longest shared prefix *length in blocks* with what this instance recently served.

        Returns 0 when nothing matches or everything expired.
        """
        if not digests:
            return 0
        ring = self._seen.get(instance_id)
        if not ring:
            return 0
        self._expire(ring)
        best = 0
        for _, resident in ring:
            shared = 0
            for digest in digests:  # the *leading* run, not just any intersection
                if digest not in resident:
                    break
                shared += 1
            if shared > best:
                best = shared
                if best == len(digests):
                    break
        return best

    def best_instance(
        self, digests: list[str], candidates: list[uuid.UUID | str]
    ) -> tuple[uuid.UUID | str, int] | None:
        """(instance, overlap) with the largest overlap among candidates, or None."""
        if not digests or not candidates:
            return None
        best: tuple[uuid.UUID | str, int] | None = None
        for candidate in candidates:
            shared = self.overlap(candidate, digests)
            if shared and (best is None or shared > best[1]):
                best = (candidate, shared)
        return best

    def note_lookup(self, matched: bool) -> None:
        self.lookups += 1
        if matched:
            self.matches += 1

    @property
    def hit_ratio(self) -> float | None:
        if not self.lookups:
            return None
        return self.matches / self.lookups

    def forget(self, instance_id: uuid.UUID | str) -> None:
        self._seen.pop(instance_id, None)

    def _expire(self, ring: deque[tuple[float, frozenset[str]]]) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        while ring and ring[0][0] < cutoff:
            ring.popleft()


# Process-wide index: the gateway is the only writer, and residency is a property of the fleet.
prefix_cache = PrefixCacheIndex()
