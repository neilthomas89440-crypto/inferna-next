# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report privately through GitHub:

1. Go to the repository’s **Security** tab → **Report a vulnerability**
   ([advisories](https://github.com/neilthomas89440-crypto/inferna-next/security/advisories/new)).
2. Include: affected version/commit, a description of the impact, reproduction steps or a proof of
   concept, and any suggested fix.

You can expect an acknowledgement within a few days. We will confirm the issue, agree on a
disclosure timeline, and credit you in the advisory unless you prefer otherwise.

## Supported versions

| Version | Supported |
|---|---|
| `0.2.x` (current `main`) | ✅ |
| older | ❌ — please retest on the latest `main` |

## Deployment assumptions (read before exposing anything)

Inferna Next is designed for a **trusted, private network**. The defaults are development
defaults; a production deployment must change them.

- **gRPC (9091) has no TLS by default.** Worker→server traffic is unauthenticated beyond the
  registration token and is plaintext. Keep the port on a private network (or a VPN/overlay); mTLS
  and version-compatibility checks are tracked in the roadmap’s release-hardening stage.
- **Change every default secret.** `INFERNA_JWT_SECRET`, `INFERNA_ADMIN_PASSWORD`,
  `INFERNA_REGISTRATION_TOKEN` ship with visible defaults; `INFERNA_ENV=production` makes the server
  fail fast if they are still in place.
- **Workers mount the Docker socket.** A worker container can control the host Docker daemon, so
  the worker host must be one you trust; run workers bare-metal on GPU hosts where possible.
- **The gateway is a proxy.** `services/upstream_guard.py` validates every worker-registered
  upstream address (allowlist via `INFERNA_GATEWAY_UPSTREAM_ALLOWLIST`, or private-range policy
  depending on `INFERNA_ENV`) and pins resolved IPs against DNS rebinding. Never bypass it when
  touching routing code.
- **Instance ports (8010–8100) are not authenticated.** Anything that can reach a worker host can
  talk to a model instance directly. Firewall these ports; use the gateway (`/v1`) with API keys
  for external access.
- **Engine containers run third-party images** (vLLM, SGLang) with host GPUs. Pin the image tags
  (`INFERNA_VLLM_IMAGE`, `INFERNA_SGLANG_IMAGE`) and review upstream advisories yourself; model
  weights and their licenses are the user’s responsibility.

## What is in scope

- Authentication and authorization (JWT handling, RBAC on `/api/v1`, API-key verification in the
  gateway).
- The SSRF guard and upstream routing in `services/upstream_guard.py` and `api/gateway.py`.
- Request handling in the control plane and worker (injection, path traversal, resource
  exhaustion, unsafe deserialization).
- Container lifecycle code that runs privileged host operations (Docker socket usage, mounts,
  port binding).

## What is out of scope

- Misconfiguration of the deployment environment (open ports, default secrets left in place,
  workers on untrusted hosts) by operators who ignored the assumptions above.
- Vulnerabilities in third-party engine images or model artifacts — report those upstream.
- Denial of service from within your own trusted network, unless it is remotely reachable through
  the gateway without credentials.