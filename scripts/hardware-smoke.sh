#!/usr/bin/env bash
set -euo pipefail

# Hardware smoke: deploy each (model × supported engine) on a real NVIDIA node,
# verify it reaches running and serves /v1/models, then stop/delete.
# Env: INFERNA_API (e.g. http://<server>:8000), INFERNA_ADMIN_TOKEN (JWT)
# Deps: curl, jq

: "${INFERNA_API:?set INFERNA_API=http://<server>:8000}"
: "${INFERNA_ADMIN_TOKEN:?set INFERNA_ADMIN_TOKEN=<jwt>}"

command -v curl >/dev/null || { echo "curl required"; exit 1; }
command -v jq >/dev/null || { echo "jq required"; exit 1; }

API="${INFERNA_API%/}"
HDR="Authorization: Bearer ${INFERNA_ADMIN_TOKEN}"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

echo "==> fetching catalog and clusters"
MODELS_JSON="$(curl -fsS -H "$HDR" "$API/api/v1/models")"
CLUSTERS_JSON="$(curl -fsS -H "$HDR" "$API/api/v1/clusters")"
CLUSTER_ID="$(echo "$CLUSTERS_JSON" | jq -r '.[0].id')"
if [ -z "$CLUSTER_ID" ] || [ "$CLUSTER_ID" = "null" ]; then
  echo "no cluster found"
  exit 1
fi
echo "cluster: $CLUSTER_ID"
echo "$MODELS_JSON" | jq -r '.[] | "\(.name) \(.category) \(.supported_engines|join(","))"' | head -20

overall_fail=0
# Collect per-cell results for engine-matrix.md
declare -a REPORT_LINES=()

poll_until() {
  local id="$1" expect="$2" timeout_s="$3"
  local deadline=$(( $(date +%s) + timeout_s ))
  while true; do
    local st
    st="$(curl -fsS -H "$HDR" "$API/api/v1/model-instances/$id" | jq -r '.state')"
    local detail
    detail="$(curl -fsS -H "$HDR" "$API/api/v1/model-instances/$id" | jq -r '.error_detail // empty')"
    echo "  state=$st"
    if [ "$st" = "$expect" ]; then
      return 0
    fi
    if [ "$st" = "error" ]; then
      echo "  error_detail: $detail"
      return 1
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "  timeout waiting for $expect"
      return 1
    fi
    sleep 15
  done
}

# Iterate over models
echo "$MODELS_JSON" | jq -c '.[]' | while read -r model; do
  mname="$(echo "$model" | jq -r '.name')"
  mid="$(echo "$model" | jq -r '.id')"
  category="$(echo "$model" | jq -r '.category')"
  engines="$(echo "$model" | jq -r '.supported_engines[]?' )"
  if [ -z "$engines" ]; then
    echo "SKIP $mname ($category) — no supported engines (unsupported)"
    REPORT_LINES+=("$mname | $category | (none) | unsupported | —")
    continue
  fi
  for engine in $(echo "$model" | jq -r '.supported_engines[]'); do
    echo "=== TEST $mname [$engine] ($category) ==="
    # deploy auto
    resp="$(curl -fsS -H "$HDR" -H "Content-Type: application/json" -X POST "$API/api/v1/model-instances" \
      -d "{\"model_id\":\"$mid\",\"cluster_id\":\"$CLUSTER_ID\",\"engine\":\"$engine\",\"profile\":\"latency\",\"gpu_selection\":\"auto\"}" || true)"
    iid="$(echo "$resp" | jq -r '.id // empty')"
    if [ -z "$iid" ]; then
      echo "  deploy failed: $resp"
      overall_fail=1
      REPORT_LINES+=("$mname | $category | $engine | fail | deploy 400: $resp")
      continue
    fi
    echo "  instance $iid"
    # poll running (10 min)
    if ! poll_until "$iid" "running" 600; then
      echo "  FAIL $mname [$engine] did not reach running"
      overall_fail=1
      REPORT_LINES+=("$mname | $category | $engine | fail | not running")
      # try to cleanup
      curl -fsS -H "$HDR" -X DELETE "$API/api/v1/model-instances/$iid" || true
      continue
    fi
    # get port and worker_name
    port="$(curl -fsS -H "$HDR" "$API/api/v1/model-instances/$iid" | jq -r '.port')"
    worker_name="$(curl -fsS -H "$HDR" "$API/api/v1/model-instances/$iid" | jq -r '.worker_name // empty')"
    echo "  port=$port worker=$worker_name"
    if [ -n "$worker_name" ] && [ -n "$port" ] && [ "$port" != "null" ]; then
      # health check engine endpoint
      if curl -fsS "http://$worker_name:$port/v1/models" >/dev/null; then
        echo "  engine /v1/models OK"
      else
        echo "  engine /v1/models failed"
        overall_fail=1
        REPORT_LINES+=("$mname | $category | $engine | fail | engine health")
      fi
    else
      echo "  skip engine health (no worker/port)"
    fi
    # stop
    curl -fsS -H "$HDR" -X POST "$API/api/v1/model-instances/$iid/stop" >/dev/null || true
    if ! poll_until "$iid" "stopped" 120; then
      echo "  FAIL stop did not reach stopped"
      overall_fail=1
    fi
    # delete
    curl -fsS -H "$HDR" -X DELETE "$API/api/v1/model-instances/$iid" >/dev/null || true
    echo "  cleaned up"
    REPORT_LINES+=("$mname | $category | $engine | pass | $(date -I) $worker_name")
  done
done

echo ""
echo "=== REPORT (copy to docs/engine-matrix.md) ==="
for line in "${REPORT_LINES[@]}"; do
  echo "$line"
done

if [ "$overall_fail" -ne 0 ]; then
  echo "hardware smoke: FAIL"
  exit 1
fi
echo "hardware smoke: PASS"
