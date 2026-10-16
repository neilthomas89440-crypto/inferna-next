# Evidence — 06 M7 parallel groups in the UI (`w3/frontend-parallel`)

The M7 gate says "the reason a rank failed is visible in the UI without SSH", and a multi-node deploy
has to be *possible* from the UI at all. This slice is the last piece of that.

## What the UI now does

| Surface | Behaviour |
|---|---|
| Deploy dialog | `Nodes per replica` (1..8) + a `pp / tp / dp` selector that appears only above one node; validates before calling the API (manual placement refused for multi-node, more than one replica group refused) and sends `nodes_per_replica`/`parallel_mode` (explicit `single` when 1, so the server's pre-multi-node contract stays visible) |
| Instance row | A rank badge next to the host: `head / 2` (emerald) or `rank 1 / 2` (slate), with the rendezvous in the title; a follower shows `→ 10.0.0.1:8110` in the port cell — it has no port of its own and says where it points |
| Endpoint control | Only for the head: a follower has no API server, and the gateway never routes to one |
| Types | `Instance` gains `parallel_group_id`, `node_rank`, `world_size`, `parallel_mode`, `rendezvous`; `DeployRequest` gains `nodes_per_replica`, `parallel_mode` |

## Verification

| Gate | Result |
|---|---|
| `cd frontend && npm run lint` | clean |
| `cd frontend && npx tsc --noEmit` | clean |
| `cd frontend && npx vitest run` | 29 passed (24 + 5 new) |
| `npx playwright test e2e/deploy-multinode.spec.ts` | error path asserted in CI; happy path gated on `INFERNA_E2E_MULTINODE=1` (needs the three-worker compose overlay) |
| M7 gate on hardware ("rank failure visible without SSH") | **BLOCKED: no GPU hosts** — the log panel from stage 2 renders the per-rank tail the worker reports (`?live=true` per rank), which is the mechanism the gate needs |

New tests: deploy dialog (single-node payload stays explicit; multi-node sends
`nodes_per_replica=2` + the chosen mode; manual placement refused before the API call — the placement
contradiction is checked *before* "pick a GPU", because it is the more fundamental error); instance
rows (head and follower badges, rendezvous shown, exactly one Endpoint control, single-node rows keep
their old presentation).

## E2E design note

The default e2e stack runs **one** worker, so a two-rank deploy cannot succeed there — and a spec that
silently expected success would either fail or have to be skipped forever. The spec therefore asserts
what the stack *can* prove: the dialog asks for the host count, the mode selector appears, and the
server's refusal (`needs 2 hosts …` / `routable address …`) is shown verbatim in the dialog instead of
the dialog closing. The happy path (two ranks as one group, endpoint only on the head, then cleanup)
runs when `INFERNA_E2E_MULTINODE=1` and the three-worker overlay is up, which is the local/hardware
procedure in `docs/network-matrix.md`.