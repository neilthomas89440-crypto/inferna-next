#!/usr/bin/env bash
# Network conditions between ranks — the M3 profiles used to publish RTT/bandwidth envelopes.
#
#   sudo scripts/netem.sh apply dc-2ms          # 2 ms RTT, no loss
#   sudo scripts/netem.sh apply wan-25ms-1%     # 25 ms RTT, 1 % loss (inter-region)
#   sudo scripts/netem.sh clear                 # remove all rules again
#   scripts/netem.sh probes dc-2ms              # show the profile definition
#
# `tc` shapes *egress* on one host, so with a single-machine stack the rules are applied to the
# loopback/bridge interface that carries the rank traffic (default `eth0`; override with
# INFERNA_NETEM_IFACE). With two machines, run the same command on both with the peer's NIC.
#
# Requires: iproute2 + root. Not needed on Windows; the M3 envelope numbers are recorded on Linux
# hosts in docs/network-matrix.md.
set -euo pipefail

IFACE="${INFERNA_NETEM_IFACE:-eth0}"
ACTION="${1:-}"

declare -A PROFILES=(
  [dc-0.1ms]="delay 0.05ms"
  [dc-2ms]="delay 1ms"
  [wan-25ms-1%]="delay 12.5ms loss 1%"
)

usage() {
  echo "usage: $0 {apply <profile>|clear|probes <profile>|list}" >&2
  echo "profiles: ${!PROFILES[*]}" >&2
  exit 2
}

require_root() {
  if [ "$(id -u)" != "0" ]; then
    echo "netem needs root (tc on $IFACE)" >&2
    exit 2
  fi
}

case "$ACTION" in
  apply)
    PROFILE="${2:-}"
    [ -n "$PROFILE" ] || usage
    [ -n "${PROFILES[$PROFILE]:-}" ] || { echo "unknown profile: $PROFILE" >&2; exit 2; }
    require_root
    tc qdisc del dev "$IFACE" root 2>/dev/null || true
    # shellcheck disable=SC2086  # the profile is a deliberate flag list
    tc qdisc add dev "$IFACE" root netem ${PROFILES[$PROFILE]}
    echo "applied '$PROFILE' on $IFACE: ${PROFILES[$PROFILE]}"
    tc qdisc show dev "$IFACE"
    ;;
  clear)
    require_root
    tc qdisc del dev "$IFACE" root 2>/dev/null || true
    echo "cleared netem on $IFACE"
    ;;
  probes)
    PROFILE="${2:-}"
    [ -n "${PROFILES[$PROFILE]:-}" ] || { echo "unknown profile: $PROFILE" >&2; exit 2; }
    echo "profile '$PROFILE' = ${PROFILES[$PROFILE]} (one-way; RTT is twice the delay)"
    echo "verify with: ping -c 20 <peer> && scripts/wan-envelope.sh iperf"
    ;;
  list)
    for name in "${!PROFILES[@]}"; do echo "$name -> ${PROFILES[$name]}"; done
    ;;
  *)
    usage
    ;;
esac