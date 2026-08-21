# Engine matrix — image tag × GPU vendor × model category

Source of truth for which `engine`/`image` combinations are supported. Statuses change only after a hardware smoke run (`scripts/hardware-smoke.sh`).

| Engine | Image tag | Vendor | Category | Status | Evidence |
|---|---|---|---|---|---|
| vllm | `vllm/vllm-openai:v0.8.5` | nvidia | llm | unverified | — |
| sglang | `lmsysorg/sglang:v0.4.6.post1` | nvidia | llm | unverified | — |
| vllm | `vllm/vllm-openai:v0.8.5` | nvidia | embedding | unverified | — |
| sglang | `lmsysorg/sglang:v0.4.6.post1` | nvidia | embedding | unverified | — |
| vllm | `vllm/vllm-openai:v0.8.5` | nvidia | multimodal | unverified | — |
| sglang | `lmsysorg/sglang:v0.4.6.post1` | nvidia | multimodal | unverified | — |
| sglang | `lmsysorg/sglang:v0.4.6.post1` | nvidia | reranker | unverified | — |
| vllm | `vllm/vllm-openai:v0.8.5` | nvidia | reranker | unsupported | pinned images don't serve reranker category |
| — | — | nvidia | audio | unsupported | pinned images are `vllm/vllm-openai:v0.8.5` and `lmsysorg/sglang:v0.4.6.post1` — they don't serve `openai/whisper-large-v3`; deploy returns 400 |
| vllm | `vllm/vllm-openai:v0.8.5` | amd | * | unsupported | CUDA images, AMD not supported |
| sglang | `lmsysorg/sglang:v0.4.6.post1` | amd | * | unsupported | CUDA images, AMD not supported |
| vllm | `vllm/vllm-openai:v0.8.5` | mock | llm/embedding/reranker/audio/multimodal | verified | dev/mock, 2026-08-21, local |
| sglang | `lmsysorg/sglang:v0.4.6.post1` | mock | llm/embedding/reranker/audio/multimodal | verified | dev/mock, 2026-08-21, local |

Legend: `verified` — hardware smoke passed on the listed evidence; `unverified` — expected to work but not yet smoked; `unsupported` — intentionally not supported (400 on deploy).

## Rules

- Changing `worker/inferna_worker/config.py` (`vllm_image`, `sglang_image`) **requires** a new hardware smoke run and an update to this file (statuses + evidence column).
- `server/inferna_server/fixtures/catalog.json` field `engines` per model **must** match this matrix. If a cell changes, update both.
- Updating statuses: run `INFERNA_API=http://<server>:8000 INFERNA_ADMIN_TOKEN=<jwt> bash scripts/hardware-smoke.sh` on the target NVIDIA node; copy the printed table block into this file and set the new `verified`/`unsupported` values.
- `mock` row is always `verified` (dev, no GPU).
