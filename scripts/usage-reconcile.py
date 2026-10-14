#!/usr/bin/env python3
"""Reconcile the gateway Prometheus counters with the usage records of ``GET /api/v1/usage``.

Measures a window: snapshots ``/metrics`` and ``/api/v1/usage`` (after giving the merge
interval time to flush), waits for traffic, then snapshots again and compares the deltas.
Exits non-zero when the totals drift beyond the tolerance or when no traffic was seen.

Usage:
    python scripts/usage-reconcile.py --api http://localhost:8000 --token "$INFERNA_ADMIN_TOKEN" \\
        --traffic-cmd 'for i in $(seq 1 20); do curl -s -o /dev/null -H "Authorization: Bearer $KEY" \\
            -H "Content-Type: application/json" -d "{\\"model\\":\\"$MODEL\\",\\"messages\\":[{\\"role\\":\\"user\\",
            \\"content\\":\\"hi\\"}],\\"stream\\":true}" "$API/v1/chat/completions"; sleep 2; done'

`--traffic-cmd` is run and awaited by this script, so the counters are quiet before the window
opens and stop before the post-window snapshot — without it the comparison is meaningless
while traffic keeps arriving.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TOLERANCE_PCT = 1.0
# Records are folded every 30 s (services/usage.USAGE_FLUSH_INTERVAL_SECONDS); a snapshot is
# only comparable once the pending buffer has been written, which the waiter detects.
DEFAULT_STABILIZE_TIMEOUT = 120.0
# One flush interval (30 s) plus slack: guarantees the buffered tail is written before the
# snapshot, so the comparison measures accounting, not the flush schedule.
DEFAULT_SETTLE_SECONDS = 35.0


def _get(url: str, token: str) -> str:
    request = urllib.request.Request(url)
    # `--token "$(…)"` from a Windows shell may carry a trailing CR; strip it here so the
    # header value is always legal.
    token = token.strip()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode()


def _counter(metrics_text: str, name: str, extra_label: str | None = None) -> float:
    """Sum one counter family; `extra_label` matches a substring inside the label set."""
    total = 0.0
    for line in metrics_text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        if extra_label is not None and extra_label not in line:
            continue
        try:
            total += float(line.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
    return total


def _snapshot(api: str, token: str, days: int) -> dict[str, float]:
    metrics_text = _get(f"{api}/metrics", token)
    usage = json.loads(_get(f"{api}/api/v1/usage?days={days}", token))["totals"]
    return {
        "gateway_requests": _counter(metrics_text, "inferna_requests_total"),
        "gateway_tokens": _counter(metrics_text, "inferna_tokens_total"),
        "usage_events": _counter(metrics_text, "inferna_usage_events_total"),
        "records_requests": float(usage["requests"]),
        "records_tokens": float(usage["tokens_in"] + usage["tokens_out"]),
    }


def _await_stable(api: str, token: str, days: int, timeout: float, poll: float = 3.0) -> dict[str, float]:
    """Snapshot repeatedly until *both* the counters and the records stop moving.

    The gateway buffers request aggregates and folds them once per interval, so a snapshot
    taken right after traffic undercounts the records by up to one interval — an artefact of
    the flush schedule, not a loss. Waiting for stable readings on both sides removes it, and
    it also tells the caller that traffic really has stopped: if requests keep arriving, the
    counters keep moving and this times out (the comparison would be meaningless).
    """
    deadline = time.time() + timeout
    previous: dict[str, float] | None = None
    stable = 0
    snapshot = _snapshot(api, token, days)
    while time.time() < deadline:
        unchanged = previous is not None and all(
            snapshot[key] == previous[key]
            for key in ("gateway_requests", "records_requests", "records_tokens")
        )
        if unchanged:
            stable += 1
            if stable >= 2:
                return snapshot
        else:
            stable = 0
        previous = snapshot
        time.sleep(poll)
        snapshot = _snapshot(api, token, days)
    print(
        "warning: counters kept moving until the timeout — is traffic still running?",
        file=sys.stderr,
    )
    return snapshot


def _run_traffic(command: str) -> int:
    """Run the caller's traffic generator and wait for it to finish."""
    print(f"running traffic command: {command}", flush=True)
    return subprocess.call(command, shell=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000", help="server base URL")
    parser.add_argument("--token", required=True, help="admin JWT for /metrics and /api/v1/usage")
    parser.add_argument("--days", type=int, default=1, help="usage window in days (default: 1)")
    parser.add_argument(
        "--stabilize-timeout",
        type=float,
        default=DEFAULT_STABILIZE_TIMEOUT,
        help="max seconds to wait for records to stop growing before a snapshot (default: 120)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        help="fixed wait before each snapshot, one flush interval plus slack (default: 35)",
    )
    parser.add_argument(
        "--traffic-cmd",
        default=None,
        help="command that generates traffic for the window (run and awaited by this script); "
        "without it the window just observes whatever traffic is running",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=30.0,
        help="seconds of traffic between the snapshots (default: 30)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE_PCT,
        help="allowed relative drift in percent (default: 1)",
    )
    args = parser.parse_args()

    # Two-stage wait: a full flush interval guarantees the buffer tail reached the records,
    # the stability poll then absorbs scheduling jitter (the flush loop ticks on its own).
    print(f"waiting {args.settle:.0f}s + stability for the pre-window records …", flush=True)
    time.sleep(args.settle)
    before = _await_stable(args.api, args.token, args.days, args.stabilize_timeout)
    if args.traffic_cmd:
        exit_code = _run_traffic(args.traffic_cmd)
        if exit_code != 0:
            print(f"traffic command failed with exit {exit_code}", file=sys.stderr)
            return 2
    elif args.window > 0:
        print(f"generating/observing traffic for {args.window:.0f}s …", flush=True)
        time.sleep(args.window)
    print(f"waiting {args.settle:.0f}s + stability for the post-window records …", flush=True)
    time.sleep(args.settle)
    after = _await_stable(args.api, args.token, args.days, args.stabilize_timeout)

    delta = {key: after[key] - before[key] for key in before}
    print(
        f"\npre-window : metrics requests={before['gateway_requests']:.0f} "
        f"records requests={before['records_requests']:.0f}"
    )
    print(
        f"post-window: metrics requests={after['gateway_requests']:.0f} "
        f"records requests={after['records_requests']:.0f}"
    )
    rows = [
        ("gateway requests", delta["gateway_requests"], delta["records_requests"]),
        ("gateway tokens", delta["gateway_tokens"], delta["records_tokens"]),
        ("usage events", delta["usage_events"], delta["records_requests"]),
    ]

    print("\n| signal | /metrics delta | usage records delta | drift |")
    print("|---|---|---|---|")
    failed = False
    for label, observed, recorded in rows:
        drift = abs(observed - recorded) / max(observed, 1.0) * 100
        print(f"| {label} | {observed:.0f} | {recorded:.0f} | {drift:.2f}% |")
        if drift > args.tolerance:
            failed = True

    if delta["gateway_requests"] == 0:
        print("\nNo gateway traffic in the window — nothing to reconcile.", file=sys.stderr)
        return 2
    if failed:
        print(
            f"\nFAIL: drift above {args.tolerance:.2f}% (buffer loss or a path that skips accounting).",
            file=sys.stderr,
        )
        return 1
    print(f"\nOK: totals agree within {args.tolerance:.2f}%.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as exc:  # pragma: no cover - operational failure path
        print(f"cannot reach the server: {exc}", file=sys.stderr)
        sys.exit(2)