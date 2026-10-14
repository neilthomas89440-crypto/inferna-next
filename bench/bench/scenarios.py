"""Load shapes for the harness: single sweep, agentic loop, long context, TTFT-only.

Every case is deterministic (temperature 0, fixed filler text) so two runs of the same
command are comparable; the filler is generated from a seeded pattern, not from a random
source, so token counts stay stable across machines.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

CONCURRENCY_SWEEP = (1, 4, 8, 16)
LONG_CONTEXT_SIZES = (32_000, 128_000)
AGENTIC_ITERATIONS = 8
AGENTIC_PREFIX_TOKENS = 4_000
# ~4 characters per token is the usual rule of thumb for English filler.
CHARS_PER_TOKEN = 4

WORDS = [
    "cluster",
    "inference",
    "gpu",
    "kernel",
    "latency",
    "throughput",
    "prefix",
    "cache",
    "replica",
    "rendezvous",
    "shard",
    "tensor",
    "pipeline",
    "engine",
    "worker",
    "scheduler",
    "gateway",
    "token",
    "stream",
    "prompt",
    "context",
    "batch",
]


def filler_tokens(tokens: int, *, seed: int) -> str:
    """Deterministic text of roughly `tokens` tokens (words repeated, not random)."""
    rng = random.Random(seed)
    words = [WORDS[rng.randrange(len(WORDS))] for _ in range(tokens)]
    return " ".join(words)


@dataclass(slots=True)
class Case:
    """One measurable unit: a label, the messages to send and the output cap."""

    label: str
    messages: list[dict]
    max_tokens: int = 128
    concurrency: int = 1


def single_cases(max_tokens: int = 128) -> list[Case]:
    """Short prompts across the concurrency sweep (1/4/8/16)."""
    prompt = "Summarise what a GPU cluster scheduler does in one sentence."
    return [
        Case(
            label=f"single conc={conc}",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            concurrency=conc,
        )
        for conc in CONCURRENCY_SWEEP
    ]


def agentic_cases(iterations: int = AGENTIC_ITERATIONS, *, max_tokens: int = 128) -> list[Case]:
    """Growing prefix, as an agent loop does: the same conversation keeps being resent.

    Each iteration appends its own prompt to the history, so the prefix (and therefore the
    prefix-cache hit potential) grows monotonically.
    """
    prefix = filler_tokens(AGENTIC_PREFIX_TOKENS, seed=42)
    messages: list[dict] = [
        {"role": "system", "content": f"Repository context:\n{prefix}"},
        {"role": "user", "content": "Reply with a single word: ready."},
    ]
    cases: list[Case] = []
    for iteration in range(iterations):
        messages = [
            *messages,
            {"role": "user", "content": f"iteration {iteration}: summarise the change"},
        ]
        cases.append(
            Case(
                label=f"agentic-loop iter={iteration}",
                messages=list(messages),
                max_tokens=max_tokens,
            )
        )
    return cases


def long_context_cases(
    sizes: tuple[int, ...] = LONG_CONTEXT_SIZES, *, max_tokens: int = 128
) -> list[Case]:
    """Long prompts (32k and 128k tokens by default) with a question at the end."""
    cases: list[Case] = []
    for size in sizes:
        body = filler_tokens(size, seed=7)
        cases.append(
            Case(
                label=f"long-context tokens={size}",
                messages=[{"role": "user", "content": f"{body}\n\nQuestion: what is this about?"}],
                max_tokens=max_tokens,
            )
        )
    return cases


def ttft_cases(*, concurrency: int = 1) -> list[Case]:
    """First-token latency only: one token of output."""
    prompt = "Say OK."
    return [
        Case(
            label="ttft-only",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1,
            concurrency=concurrency,
        )
    ]


def build_cases(scenario: str, *, concurrency: int, max_tokens: int = 128) -> list[Case]:
    """Cases for a scenario name, with the CLI's concurrency applied where it makes sense."""
    match scenario:
        case "single":
            return [
                Case(
                    label=case.label,
                    messages=case.messages,
                    max_tokens=case.max_tokens,
                    concurrency=concurrency if concurrency > 0 else case.concurrency,
                )
                for case in single_cases(max_tokens)
            ]
        case "agentic-loop":
            return agentic_cases(max_tokens=max_tokens)
        case "long-context":
            return long_context_cases(max_tokens=max_tokens)
        case "ttft-only":
            return ttft_cases(concurrency=concurrency if concurrency > 0 else 1)
        case _:
            raise ValueError(f"unknown scenario: {scenario}")
