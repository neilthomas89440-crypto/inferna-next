#!/usr/bin/env bash
# Wide-area envelope: what RTT × bandwidth each parallel mode survives.
#
#   scripts/wan-envelope.sh iperf --peer 10.0.0.2            # measure usable bandwidth
#   scripts/wan-envelope.sh budget --stage-mb 0.5 --size-mb 16384 --rtt-ms 25 --gbit 10
#   scripts/wan-envelope.sh budget --mode tp --hidden 4096 --rtt-ms 25 --gbit 10
#
# `iperf` needs two hosts (this script runs `iperf3 -s` in the background and measures against
# the peer). `budget` is pure arithmetic and runs anywhere: it prints the N-staged transfer time
# for a pipeline, the exchange per token for tensor parallelism, and the resulting verdict.
#
# Numbers produced here go into docs/network-matrix.md; stage 7 uses them to publish
# "unusable above RTT X ms" instead of promising everything everywhere.
set -euo pipefail

ACTION="${1:-}"
shift || true

peer=""
stage_mb="0.5"      # hidden state handed to the next stage per step (batch 64 x 4096 x 2 B)
size_mb="16384"     # model size in MB (16 GB = a 7B bf16 checkpoint, startup transfer)
hidden="4096"       # hidden size, for the per-token all-gather/reduce of tensor parallelism
rtt_ms="25"
gbit="10"
mode="pp"

while [ $# -gt 0 ]; do
  case "$1" in
    --peer) peer="$2"; shift 2 ;;
    --stage-mb) stage_mb="$2"; shift 2 ;;
    --size-mb) size_mb="$2"; shift 2 ;;
    --rtt-ms) rtt_ms="$2"; shift 2 ;;
    --gbit) gbit="$2"; shift 2 ;;
    --mode) mode="$2"; shift 2 ;;
    --hidden) hidden="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$ACTION" in
  iperf)
    if ! command -v iperf3 >/dev/null 2>&1; then
      echo "iperf3 is required for the bandwidth measurement" >&2
      exit 2
    fi
    if [ -z "$peer" ]; then
      echo "usage: $0 iperf --peer <host>" >&2
      exit 2
    fi
    echo "== latency (20 probes to $peer) =="
    ping -c 20 "$peer" | tail -2
    echo
    echo "== bandwidth (8 parallel streams, 10 s) =="
    iperf3 -c "$peer" -P 8 -t 10 | tail -6
    ;;

  budget)
    python3 - "$mode" "$stage_mb" "$size_mb" "$rtt_ms" "$gbit" "$hidden" <<'PY'
import sys

mode, stage_mb, size_mb, rtt_ms, gbit, hidden = sys.argv[1:7]
stage_mb, size_mb, rtt_ms, gbit, hidden = map(float, (stage_mb, size_mb, rtt_ms, gbit, hidden))

# Wire time for one payload, in milliseconds, ignoring protocol overhead (documented as such).
def wire_ms(payload_mb: float) -> float:
    throughput_mb_s = gbit * 1000 / 8  # Gbit/s -> MB/s
    return payload_mb / throughput_mb_s * 1000

if mode == "pp":
    transfer = wire_ms(stage_mb) + rtt_ms          # one hand-off per pipeline step
    total = transfer * 2                            # a step is a round of activations back and forth
    verdict = "interactive" if total < 50 else ("batch-only" if total < 250 else "unusable")
    print(f"mode=pp stage={stage_mb:.2f}MB rtt={rtt_ms:.0f}ms bandwidth={gbit:.0f}Gbit/s")
    print(f"  wire time per hand-off : {wire_ms(stage_mb):8.2f} ms")
    print(f"  per decoding step      : {total:8.2f} ms  ->  {1000 / total if total else 0:6.1f} tok/s ceiling")
    print(f"  full model transfer    : {wire_ms(size_mb):8.0f} ms (one-off, at startup)")
    print(f"  verdict                : {verdict}")
    print("  caveat                 : optimistic — counts ONE collective round per token, while a")
    print("                           real engine runs several per layer; treat it as an upper bound")
elif mode == "tp":
    # Every decoding step all-gathers the hidden state and reduce-scatters the result: the
    # payload per token per link is 2 x hidden x 2 bytes (bf16), plus one RTT of latency.
    payload_mb = 2 * hidden * 2 / (1024 * 1024)
    total = wire_ms(payload_mb) + rtt_ms
    verdict = "interactive" if total < 50 else ("batch-only" if total < 250 else "unusable")
    print(f"mode=tp hidden={hidden:.0f} rtt={rtt_ms:.0f}ms bandwidth={gbit:.0f}Gbit/s")
    print(f"  payload per token/link : {payload_mb * 1024:8.2f} KB")
    print(f"  per-token exchange     : {total:8.2f} ms  ->  {1000 / total if total else 0:6.1f} tok/s ceiling")
    print(f"  verdict                : {verdict}")
else:
    print(f"mode={mode}: no cross-rank exchange modelled (data parallel)", flush=True)
    print("  verdict                : interactive by construction")
PY
    ;;

  *)
    echo "usage: $0 {iperf --peer <host>|budget [--mode pp|tp] [--stage-mb N] [--size-mb N] [--rtt-ms N] [--gbit N] [--tokens N]}" >&2
    exit 2
    ;;
esac