# Evidence — 06 M9 release hardening (`w5/release-hardening`)

The last milestone’s implementable half: transport security, the version gate’s first tests, and the
release documentation. The hardware half stays blocked, and the `v1.0` tag is deliberately **not**
applied — see the end of this file.

## What landed

| Piece | Behaviour |
|---|---|
| gRPC TLS/mTLS | `INFERNA_GRPC_TLS_CERT/KEY/CA` + `…_REQUIRE_CLIENT_CERT` on the server: `serve_grpc` binds `add_secure_port` when a certificate is configured, plaintext otherwise (with an explicit warning in production pointing at the alternative) |
| Fail-fast material check | certificate without key, or a client-cert requirement without a CA, is a **startup** error (`Settings` validator) — the operator’s cheapest moment to fix it |
| Worker trust | `INFERNA_SERVER_TLS_CA` (+ `…_TLS_AUTHORITY` for name override) makes the worker use `grpc.secure_channel`; empty CA keeps the documented plaintext private-network default |
| Version gate | the `Register` compatibility gate (`PROTOCOL_VERSION`, `MIN_WORKER_VERSION`) is now covered by tests, including an unparsable worker version being treated as too old rather than trusted |
| `CHANGELOG.md` | new: stage 0–6 released history plus the unreleased stage-7 milestones, each pointing at its evidence file |
| README “Reproducing the release checks” | the four scripted gates with concrete commands, the environment rule (“a fixture is never a verdict”), and an explicit list of what is still blocked |

## Verification

| Gate | Result |
|---|---|
| `cd server && uv run ruff check .` | `All checks passed!` |
| `cd server && uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| `cd server && uv run pytest -q` | 253 passed, 2 skipped (245 + 8 new) |
| `cd worker && uv run ruff check . && uv run pyright && uv run pytest -q` | clean; 44 passed |
| TLS is a real handshake, not a config assertion | a self-signed certificate (generated with `openssl` in the test) serves the actual servicer, and a client trusting that CA gets `PERMISSION_DENIED` for a wrong token — i.e. the RPC arrived, so the credentials are wired correctly. A wrong wiring would fail with `UNAVAILABLE` |
| port selection | a recording fake server proves `serve_grpc` binds **secure** with a certificate and **insecure** without one |

New tests (`server/tests/test_transport_security.py`, 8): unsupported protocol version rejected
(FAILED_PRECONDITION); too-old and unparsable worker versions rejected; the current versions accepted;
TLS settings require a matching key and CA; no credentials without material; unreadable files fail
fast; the real TLS handshake against the servicer; secure-vs-insecure port selection.

## What M9 still needs (and why the tag is not here)

1. **Hardware evidence**: `docs/engine-matrix.md` (every NVIDIA cell is `unverified`) and
   `docs/phase0-validation.md` (every `Observed:` is `_TBD`) are filled by `scripts/hardware-smoke.sh`
   on real GPUs; the parity and coding reports need the same hosts to stop being harness checks.
2. **A 24-hour multi-node soak** (`scripts/soak-multinode.sh --docker`) with the fault matrix filled.
3. **Then** the version bump, `check-migrations.sh`’s `PREVIOUS_RELEASE_HEAD` move, and `v1.0`.

Tagging now would publish a release whose own gate document says “not measured”, which is exactly the
failure mode this program has been avoiding all along (mock numbers labelled as verdicts). The
commands for all three steps are in `docs/multinode-parity.md`, `docs/coding-validation.md` and
`docs/fault-matrix.md` §Status, so the first hardware session closes them without re-deriving
anything.