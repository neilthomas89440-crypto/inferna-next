#!/usr/bin/env bash
set -euo pipefail

# Soak: repeated deploy → running → (restart every 4th cycle) → stop → delete
# Env: INFERNA_API (http://<server>:8000), INFERNA_ADMIN_TOKEN (JWT), SOAK_HOURS=24, SOAK_INTERVAL_MIN=5
# Output: soak-<date>.jsonl with {ts, action, instance_id, model, engine, result, duration_s, error}

: "${INFERNA_API:?set INFERNA_API}"
: "${INFERNA_ADMIN_TOKEN:?set INFERNA_ADMIN_TOKEN}"
SOAK_HOURS="${SOAK_HOURS:-24}"
SOAK_INTERVAL_MIN="${SOAK_INTERVAL_MIN:-5}"

command -v curl >/dev/null || { echo "curl required"; exit 1; }
command -v jq >/dev/null || { echo "jq required"; exit 1; }

API="${INFERNA_API%/}"
HDR="Authorization: Bearer ${INFERNA_ADMIN_TOKEN}"
OUT="soak-$(date +%Y%m%d-%H%M%S).jsonl"
echo "soak $SOAK_HOURS h (interval $SOAK_INTERVAL_MIN min) -> $OUT"
echo "API=$API"

MODELS_JSON="$(curl -fsS -H "$HDR" "$API/api/v1/models")"
CLUSTERS_JSON="$(curl -fsS -H "$HDR" "$API/api/v1/clusters")"
CLUSTER_ID="$(echo "$CLUSTERS_JSON" | jq -r '.[0].id')"

# pick 2-3 small models that fit mock/vcr
MODEL_IDS=()
while IFS= read -r id; do MODEL_IDS+=("$id"); done < <(echo "$MODELS_JSON" | jq -r '.[] | select(.vram_required_mb <= 4096) | .id' | head -3)
if [ ${#MODEL_IDS[@]} -eq 0 ]; then
  # fallback to smallest
  MODEL_IDS=($(echo "$MODELS_JSON" | jq -r 'sort_by(.vram_required_mb) | .[0:2] | .[].id'))
fi
echo "models: ${MODEL_IDS[*]}"

poll_until() {
  local id="$1" expect="$2" timeout_s="$3"
  local deadline=$(( $(date +%s) + timeout_s ))
  while true; do
    local st
    st="$(curl -fsS -H "$HDR" "$API/api/v1/model-instances/$id" 2>/dev/null | jq -r '.state // empty')"
    if [ "$st" = "$expect" ]; then return 0; fi
    if [ "$st" = "error" ]; then
      local d
      d="$(curl -fsS -H "$HDR" "$API/api/v1/model-instances/$id" 2>/dev/null | jq -r '.error_detail // empty')"
      echo "  error: $d" >&2
      return 2
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then return 1; fi
    sleep 10
  done
}

log_jsonl() {
  # args: action instance_id model engine result duration_s error
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  jq -n --arg ts "$ts" --arg action "$1" --arg iid "$2" --arg model "$3" --arg engine "$4" --arg result "$5" --argjson dur "$6" --arg err "$7" \
    '{ts:$ts, action:$action, instance_id:$iid, model:$model, engine:$engine, result:$result, duration_s:$dur, error:$err}' >> "$OUT"
}

overall_fail=0
end_ts=$(( $(date +%s) + SOAK_HOURS * 3600 ))
cycle=0
while [ "$(date +%s)" -lt "$end_ts" ]; do
  cycle=$((cycle+1))
  mid="${MODEL_IDS[$(( (cycle-1) % ${#MODEL_IDS[@]} ))]}"
  mname="$(echo "$MODELS_JSON" | jq -r --arg id "$mid" '.[] | select(.id==$id) | .name')"
  # choose engine: first supported
  engine="$(echo "$MODELS_JSON" | jq -r --arg id "$mid" '.[] | select(.id==$id) | .supported_engines[0] // "vllm"')"
  echo "[$cycle] deploy $mname [$engine]"

  t0=$(date +%s)
  resp="$(curl -fsS -H "$HDR" -H "Content-Type: application/json" -X POST "$API/api/v1/model-instances" \
    -d "{\"model_id\":\"$mid\",\"cluster_id\":\"$CLUSTER_ID\",\"engine\":\"$engine\",\"profile\":\"latency\",\"gpu_selection\":\"auto\"}" 2>&1 || true)"
  iid="$(echo "$resp" | jq -r '.id // empty')"
  dur=$(( $(date +%s) - t0 ))
  if [ -z "$iid" ]; then
    echo "  deploy failed: $resp"
    log_jsonl "deploy" "" "$mname" "$engine" "fail" "$dur" "$resp"
    if echo "$resp" | grep -qE "409|500"; then
      echo "invariant violation: 409/500 on deploy"
      overall_fail=1
      break
    fi
    sleep $((SOAK_INTERVAL_MIN*60))
    continue
  fi
  log_jsonl "deploy" "$iid" "$mname" "$engine" "ok" "$dur" ""

  # poll running ≤10 min
  t0=$(date +%s)
  if ! poll_until "$iid" "running" 600; then
    rc=$?
    dur=$(( $(date +%s) - t0 ))
    if [ "$rc" -eq 2 ]; then
      echo "  unexpected error state"
      log_jsonl "poll_running" "$iid" "$mname" "$engine" "error" "$dur" "unexpected error"
      overall_fail=1
      break
    else
      echo "  timeout running"
      log_jsonl "poll_running" "$iid" "$mname" "$engine" "timeout" "$dur" "timeout"
      overall_fail=1
      break
    fi
  fi
  dur=$(( $(date +%s) - t0 ))
  log_jsonl "poll_running" "$iid" "$mname" "$engine" "ok" "$dur" ""
  # GET /v1/models on worker port (best effort)
  port="$(curl -fsS -H "$HDR" "$API/api/v1/model-instances/$iid" | jq -r '.port // empty')"
  worker_name="$(curl -fsS -H "$HDR" "$API/api/v1/model-instances/$iid" | jq -r '.worker_name // empty')"
  if [ -n "$port" ] && [ -n "$worker_name" ]; then
    if ! curl -fsS "http://$worker_name:$port/v1/models" >/dev/null 2>&1; then
      echo "  warning: engine /v1/models not reachable"
    fi
  fi

  # every 4th cycle restart instead of stop/delete
  if [ $((cycle % 4)) -eq 0 ]; then
    echo "  restart"
    t0=$(date +%s)
    curl -fsS -H "$HDR" -X POST "$API/api/v1/model-instances/$iid/restart" >/dev/null
    dur=$(( $(date +%s) - t0 ))
    log_jsonl "restart" "$iid" "$mname" "$engine" "ok" "$dur" ""
    t0=$(date +%s)
    if ! poll_until "$iid" "running" 600; then
      echo "  restart poll failed"
      log_jsonl "poll_restart" "$iid" "$mname" "$engine" "fail" "$(( $(date +%s)-t0 ))" "not running"
      overall_fail=1
      break
    fi
    log_jsonl "poll_restart" "$iid" "$mname" "$engine" "ok" "$(( $(date +%s)-t0 ))" ""
  fi

  # stop
  t0=$(date +%s)
  curl -fsS -H "$HDR" -X POST "$API/api/v1/model-instances/$iid/stop" >/dev/null
  dur=$(( $(date +%s) - t0 ))
  log_jsonl "stop" "$iid" "$mname" "$engine" "ok" "$dur" ""
  t0=$(date +%s)
  if ! poll_until "$iid" "stopped" 600; then
    echo "  stop poll failed"
    log_jsonl "poll_stopped" "$iid" "$mname" "$engine" "fail" "$(( $(date +%s)-t0 ))" "not stopped"
    overall_fail=1
    break
  fi
  log_jsonl "poll_stopped" "$iid" "$mname" "$engine" "ok" "$(( $(date +%s)-t0 ))" ""

  # delete
  t0=$(date +%s)
  curl -fsS -H "$HDR" -X DELETE "$API/api/v1/model-instances/$iid" >/dev/null
  dur=$(( $(date +%s) - t0 ))
  log_jsonl "delete" "$iid" "$mname" "$engine" "ok" "$dur" ""

  # invariant: check worker log for orphan/stale removal of DB-known ids (best effort via docker logs if accessible)
  # We only log locally; CI can grep.

  echo "  cycle $cycle done, sleep $SOAK_INTERVAL_MIN min"
  sleep $((SOAK_INTERVAL_MIN*60))
done

if [ "$overall_fail" -ne 0 ]; then
  echo "soak FAIL — see $OUT"
  exit 1
fi
echo "soak PASS ($cycle cycles) — see $OUT"
