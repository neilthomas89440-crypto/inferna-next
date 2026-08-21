# Phase 0 — Failure matrix & soak runbook

This file is the validation log for Phase 0 production readiness. Each scenario
must be executed on the target NVIDIA node and the observed output recorded.
Phase 0 is complete only when scenarios 0–9 pass and a 24h soak has zero
violations.

## Scenarios

### 0. Network boundary (private network)

**Steps:**

```bash
nc -z -w 5 <server-public-ip> 9091; echo $?
nc -z -w 5 <server-public-ip> 8010; echo $?
curl -fsS https://<server-public-ip>/healthz  # via reverse proxy, if any
# Dev: on localhost, `docker compose` publishes 9091 — expected open (dev artifact).
```

**Expected:** From an untrusted external host, `9091` and `8010` are
`connection refused/filtered` (firewall/security-group). Port `8000` is only
via reverse proxy. On dev `localhost` with `docker-compose.yml`, `9091` is
open — this is a documented dev artifact, not a production regress.

**Observed:** _TBD — date, node, `nc` output_

### 1. Image pull failure

```bash
# on worker host
INFERNA_VLLM_IMAGE=nonexistent.example.com/inferna:does-not-exist docker compose up -d worker
# deploy any llm model
curl -H "Authorization: Bearer $INFERNA_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"model_id":"<id>","cluster_id":"<cid>","engine":"vllm","profile":"latency","gpu_selection":"auto"}' \
  $INFERNA_API/api/v1/model-instances
# poll GET /api/v1/model-instances/<id> — expect state=error within ≤2 min, error_detail contains pull reason
```

**Expected:** `error` with `error_detail` mentioning image pull failure.

**Observed:** _TBD_

### 2. Gated model without HF token

```bash
# worker has INFERNA_HF_TOKEN=""
curl -H "Authorization: Bearer $INFERNA_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"model_id":"<Llama-3.1-8B>","cluster_id":"<cid>","engine":"vllm","profile":"latency","gpu_selection":"auto"}' \
  $INFERNA_API/api/v1/model-instances
# poll -> error, detail: "model requires HF token"
```

**Expected:** `error` state, detail exactly “model requires HF token”.

**Observed:** _TBD_

### 3. OOM — request too large + runtime OOM

**Request OOM:**

```bash
# deploy model larger than free VRAM
curl ... -d '{"model_id":"<large-vram>","cluster_id":"<cid>",...}' $INFERNA_API/api/v1/model-instances
# expect 400 "no GPU with enough free VRAM in cluster", instance not created
```

**Runtime OOM:** engine container OOMKilled → `docker inspect` shows OOM, worker reports `error` with tail of container logs (`LOG_TAIL_LINES`).

**Observed:** _TBD_

### 4. Occupied port

```bash
# on worker host
python -m http.server 8010 &
# deploy -> server should pick another port
# or, if docker create hits conflict, instance goes error with detail — both outcomes acceptable, record which occurred
```

**Expected:** Either alloc avoids `8010`, or `error` with port-conflict detail.

**Observed:** _TBD_

### 5. Docker restart

```bash
systemctl restart docker
# containers without restart policy are gone
# worker (adopt_existing) sees exited containers via labels -> reports stopped -> server (desired running) sends start -> running
# check worker log: no "removing orphaned/stale container" for DB-known instances
```

**Expected:** Instances recover to `running` without orphan deletion.

**Observed:** _TBD_

### 6. Worker restart (with and without exited container)

**With live containers:**

```bash
kill <worker-pid>  # or docker restart worker
# containers stay alive (labels)
# worker re-registers (same hostname -> reuse row, token rotated) -> adopt_existing adopts -> reports running, no delete -> running
# verify worker log: zero containers.create after restart
```

**With exited container (applied == generation):**

```bash
# after scenario 5 (docker restart) containers are exited
# server sees applied==generation but obs==stopped -> sends exactly one start -> container recreated once
# count `containers.create` in worker log == 1
```

**Observed:** _TBD_

### 7. Server restart

```bash
# stop server
docker compose stop server
# wait 30s -> workers marked disconnected, live instances -> error "worker disconnected"
curl $INFERNA_API/api/v1/model-instances | jq '.[].state'
# restart server
docker compose up -d server
# wait Sync -> running
```

**Expected:** After restart, instances converge back to `running`.

**Observed:** _TBD_

### 8. Network partition

```bash
docker network disconnect inferna-next_default worker  # or iptables -A OUTPUT -d <server-ip> -j DROP
# wait 30s -> error "worker disconnected"
# restore
docker network connect inferna-next_default worker
# wait -> running
```

**Observed:** _TBD_

### 9. Repeated registration

```bash
# run two workers with same INFERNA_WORKER_NAME/hostname on same cluster
# check DB
psql -c "select cluster_id, hostname, count(*) from workers group by cluster_id, hostname having count(*)>1"
# expect 0 rows; workers table has one row per (cluster_id, hostname), token of last instance is valid
```

**Observed:** _TBD_

## Acceptance

All scenarios 0–9 must have **observed** filled (date, node, output snippet) and
match expected. Any deviation → issue + fix before merging Phase 0.

## Soak

See `scripts/soak.sh`. Run `SOAK_HOURS=24 INFERNA_API=... INFERNA_ADMIN_TOKEN=... bash scripts/soak.sh`.

Invariants (any violation → fail):

- every command reaches terminal state ≤10 min
- no `error` outside injected failures
- no `409`/`500` on deploy
- worker log has zero `removing orphaned/stale container` for DB-known instance IDs

**Soak result:** _TBD — attach `soak-*.jsonl` and summarize_

