# Phase 1 — Inference Gateway

## Problem

The platform deploys instances but does not serve them: an instance is only
reachable as `http://<worker-hostname>:<port 8010–8100>/v1`. There is no unified
endpoint, no auth on inference (whoever reaches the port can use the GPU), and
no traffic metrics. "Model-as-a-Service" is currently "Model-as-a-Deployment".

## Goal

A single OpenAI-compatible endpoint on the server: clients point their
`base_url` at the server and consume models through the platform. Auth via API
keys, accounting via Prometheus request metrics.

## Contract

Mounted at the server root (`:8000/v1`) to be a drop-in replacement for an
OpenAI-compatible endpoint:

- `POST /v1/chat/completions` — OpenAI-format body; `model` = catalog id. The
  server resolves the model to a live instance and proxies to the worker.
- `POST /v1/embeddings`, `POST /v1/audio/transcriptions` — for embedding/audio
  categories.
- `GET /v1/models` — list of deployed models (OpenAI-compatible format).
- Auth: `Authorization: Bearer inf-<key>`.
- Streaming (SSE) is mandatory — pass-through without buffering.

## Scope

### Server

- `models.py` — `ApiKey(id, user_id, name, key_hash, scopes, last_used_at,
  created_at, revoked_at)`.
- `api/keys.py` — key CRUD: users manage their own keys, admins manage all.
  The key is shown once at creation.
- `api/gateway.py` — `/v1/*` router: resolve model → instance in `LIVE_STATES`
  (see `services/scheduler.py`), proxy via `httpx.stream` +
  `StreamingResponse`.
- `services/metrics.py` — `inferna_requests_total{model,status}`, latency/TPS
  histograms; token usage where parseable from the final usage-chunk of the
  stream.
- Alembic migration for `ApiKey`.
- `docs/openapi.yaml` — regenerate via `scripts/export-openapi.py`.

### Frontend

- `pages/ApiKeysPage.tsx` — create/revoke keys.
- Instance card — ready-made snippet: `base_url` + `curl` / client example with
  a key.

### Monitoring

- `deploy/grafana` — a "Usage" dashboard alongside "Inferna Overview".

## Decisions & risks

- **Proxy inside the server process**, no new service — consistent with the
  spec (single server, no brokers/queues). FastAPI handles hundreds of
  concurrent SSE streams.
- **Keys stored as SHA-256** — the `sha256_hex`/`generate_worker_token` pattern
  already exists in `services/workers_svc.py`.
- **Direct ports 8010–8100 remain** as an advanced mode — v1 is not broken.
- **Risk: reachability.** The server must reach worker hosts over the network;
  cloud workers need a route to the host. Works out of the box in compose/mock.
- **Risk: token counting from streams** is unreliable — count where the engine
  emits usage in the final chunk, otherwise limit to requests/latency.

## Acceptance criteria

- `curl` with a key passes `POST /v1/chat/completions`, streaming arrives in
  chunks.
- A revoked key gets 401.
- Request metrics are visible in Grafana.
- Existing e2e (auth, smoke) stays green; keys covered by unit tests.
