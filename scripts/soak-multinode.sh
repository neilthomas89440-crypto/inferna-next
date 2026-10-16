#!/usr/bin/env bash
# Multi-node soak: parallel-group lifecycle with rotating faults.
#
#   INFERNA_API=http://server:8000 INFERNA_ADMIN_TOKEN=$JWT scripts/soak-multinode.sh
#   scripts/soak-multinode.sh --cycles 1 --api http://localhost:8000 --token "$JWT"   # smoke
#
# 24 h by default; `--docker` enables fault injection (kill head/peer, restart worker over ssh);
# packet loss needs a Linux host with root (scripts/netem.sh). Invariants are asserted every cycle —
# a 409/500 from the control plane, a group that will not stop, or ranks left behind end the run
# non-zero. Output: artifacts/soak-multinode-<ts>.jsonl.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (the soak parses JSON and asserts invariants)" >&2
  exit 2
fi

hours="${SOAK_HOURS:-24}"
interval="${SOAK_INTERVAL_MIN:-5}"
extra=()
if [ -n "${INFERNA_API:-}" ] && [ -n "${INFERNA_ADMIN_TOKEN:-}" ]; then
  extra+=(--api "$INFERNA_API" --token "$INFERNA_ADMIN_TOKEN")
fi

portable_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --hours)
      hours="$2"
      shift 2
      ;;
    --interval-min)
      interval="$2"
      shift 2
      ;;
    *)
      portable_args+=("$1")
      shift
      ;;
  esac
done

exec python3 scripts/soak_multinode.py --hours "$hours" --interval-min "$interval" \
  "${extra[@]}" "${portable_args[@]}"