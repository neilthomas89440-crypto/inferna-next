# Phase 3 — Custom Models

## Problem

Deployment is only possible from `server/inferna_server/fixtures/catalog.json`.
Any other model requires editing the file and restarting the server.

## Scope

### Server

- `POST /api/v1/models` (admin): `{ hf_repo_id, category, vram_required_mb,
  requires_hf_token }`.
- `vram_required_mb` — **manual input is required** (auto-detection is
  unreliable); optionally hint from the HF API at creation time.
- Custom models are flagged `is_builtin=false`.
- Compatibility with `seed_catalog` (`services/workers_svc.py`): the seed
  upserts by name and sets `is_builtin=True` — custom models with unique names
  are untouched. Edge case: a custom model named like a builtin model gets
  overwritten on restart — reject such names at creation.

### Frontend

- "Add model" form on `ModelsPage.tsx`: repo id, category, VRAM, gated flag
  (HF token), license.

### Worker

- No changes: it already receives `model_name` and engine flags in
  `EngineConfig`.

## Decisions & risks

- **Category → engine**: validate at deploy (e.g. embedding models in vLLM are
  limited); if the engine cannot run the model it surfaces as `error` with a
  log tail (increases the value of phase 4).
- **Wrong VRAM** → scheduler 400 ("no GPU with enough free VRAM"); the user
  must be able to recreate the instance with different values.
- Deleting a custom model only allowed when it has no live instances.

## Acceptance criteria

- Deploy an arbitrary HF repo (e.g. `Qwen/Qwen2.5-3B-Instruct`) without code
  changes.
- The model appears in the catalog marked as custom; deploy/stop behave like
  builtin.
