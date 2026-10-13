## What & why

<!-- What changes, and which plan/issue does it implement? Link it: docs/roadmap/plans/… , #123 -->

## How it was verified

<!-- Paste the exact commands you ran and their outcome (e.g. "cd server && uv run pytest → 176 passed, 2 skipped"). -->

## Checklist

- [ ] Scope matches the linked plan/issue — nothing extra bundled in
- [ ] `cd server && uv run ruff check . && uv run pyright && uv run pytest` passes
- [ ] `cd worker && uv run ruff check . && uv run pyright && uv run pytest` passes (if touched)
- [ ] `cd frontend && npm run lint && npx tsc --noEmit && npx vitest run` passes (if touched)
- [ ] Generated artifacts regenerated in this PR: `bash scripts/gen-proto.sh` / `scripts/export-openapi.py`
- [ ] Migration added **with** a working `downgrade()` (if the DB schema changed)
- [ ] New/updated tests cover the observable behaviour (boundaries, error paths)
- [ ] Docs updated when behaviour changed: README tables, `docs/architecture.md`, `.env.example`, roadmap status
- [ ] No new secret, token or real HF credential in the diff
- [ ] Screenshot/recording attached for user-visible UI changes

## Notes for reviewers

<!-- Tradeoffs, risks, follow-ups explicitly deferred, things you want a second opinion on. -->