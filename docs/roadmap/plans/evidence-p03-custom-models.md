# Runtime smoke on the real mock stack

Server on SQLite (migrations `e8f2a6c41d93 -> b4d7e91c2a58 -> a5646d3ba245`), mock worker, no
Docker. Every step below is actual output, not an expectation:

```
1.  login                                       200
2.  create custom (acme/llm-3b-instruct, llm)   201  is_builtin=False  engines=['vllm','sglang']
3.  create same name again                      409  "model already exists"
4.  create with builtin name (openai/whisper…)  409  "name is reserved by a builtin model"
5.  create audio model (acme/whisper-large-v3)  201  engines=[]
6.  deploy custom model (auto)                  201  state=scheduled, deployment=02a363af…
7.  deploy audio model with engine=vllm         400  "engine 'vllm' does not support category 'audio'"
8.  instance reached                            running  port=8010
9.  delete model while deployed                 409  "model has deployments"
10. delete instance                             204
11. delete custom model                         204
12. GET a deleted model                         404  "model not found"
```

2026-10-14, branch `p03-custom-models`.

# Verification matrix (gate commands)

| Gate | Result |
|---|---|
| `cd server && uv run ruff check .` | `All checks passed!` |
| `cd server && uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| `cd server && uv run pytest -q` | `189 passed, 2 skipped` (180 + 9 new) |
| `cd worker && uv run pytest -q` | `31 passed` (unchanged — no worker code touched in this slice) |
| `cd frontend && npm run lint` | clean (new spec + tests included) |
| `cd frontend && npx tsc --noEmit` | clean |
| `cd frontend && npx vitest run` | `21 passed` (18 + 3 new) |
| `cd server && uv run python ../scripts/export-openapi.py --check` | up to date (+104 lines: POST/DELETE `/models`, `ModelCreate`) |
| `bash scripts/gen-proto.sh && git diff --exit-code` | unchanged (no proto change in this slice) |
| `bash scripts/check-migrations.sh` (Postgres) | **PASS in CI** (run 38545537698, `migrations` job) — no migration in this slice |
| `cd frontend && npx playwright test` | **PASS in CI** (run 38545537698, `e2e` job, includes the new `e2e/custom-model.spec.ts`) — the first attempt failed in all three browsers because the card renders the repo id twice (display name defaults to it), so the strict locator matched two nodes; it now targets the display-name link |

# Decisions recorded by this slice

- **Engines are derived server-side from the category**, not entered by the user:
  `{llm, embedding, multimodal} → [vllm, sglang]`, `reranker → [sglang]`, `audio → []`, matching
  `fixtures/catalog.json` and `docs/engine-matrix.md`. An `audio` model can be added but every
  deploy is rejected with 400 by the existing engine gate (`api/instances.py`), which step 7 above
  demonstrates.
- **No Hugging Face Hub lookup**: `vram_required_mb` is user input (the roadmap calls automatic
  detection optional and unreliable), `params_b` stays null for custom models.
- **Builtin names are rejected (409)** because `seed_catalog` upserts by name on every server start
  and would otherwise overwrite a custom entry; a test pins this (`test_seed_catalog_leaves_custom_models_alone`).
- **Deletion is guarded**: builtins are never deletable, and a model with any deployment returns
  409 until the deployment is deleted (steps 9–11). Deleting a custom model is allowed — otherwise
  the catalog accumulates entries with no way out.

# Files changed

- `server/inferna_server/schemas.py` — `ModelCreate` (validated: category `Literal`, VRAM bounds,
  repo id length)
- `server/inferna_server/api/models.py` — `CATEGORY_ENGINES`, `POST /api/v1/models` (admin, 201,
  409 for builtin/duplicate names incl. the `IntegrityError` race), `DELETE /api/v1/models/{id}`
  (admin, 204/404/409)
- `server/tests/test_custom_models.py` — 9 tests: derivation per category, builtin/duplicate
  conflicts, admin-only, input validation, delete guards, deploy + engine gate, seed isolation
- `frontend/src/api/hooks.ts` — `useCreateModel`, `useDeleteModel`, `ModelCreateBody`
- `frontend/src/pages/ModelsPage.tsx` — admin-only `Add model` form (repo id, display name,
  category, VRAM, license, description, gated-token checkbox), `custom` badge, per-card delete with
  `ConfirmDialog`
- `frontend/src/__tests__/ModelsPage.test.tsx` — 3 tests (POST payload, badge/delete visibility,
  non-admin read-only)
- `frontend/e2e/custom-model.spec.ts` — add → badge → deploy → running → delete on the mock stack
- `docs/openapi.yaml` — regenerated