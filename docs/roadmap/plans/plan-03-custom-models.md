# План: Custom Models (README этап 3, `docs/roadmap/03-custom-models.md`)

Слаг: `p03-custom-models`. Ветка: `p03-custom-models` от `main` (после волны 0). Идёт **после**
`plan-04-instance-logs.md` (роадмап README: этап 3 зависит от этапа 2 «Диагностика»).

## Context

Деплой возможен только из `server/inferna_server/fixtures/catalog.json`: `api/models.py` отдаёт
только `GET ""` и `GET /{id:uuid}` (`:19`, `:28`), создание модели отсутствует. Схема `Model`
(`models.py:123-144`) уже поддерживает всё нужное: `name` (HF id, unique), `display_name`,
`category` (CHECK: llm|embedding|reranker|audio|multimodal), `description`, `params_b`,
`vram_required_mb`, `requires_hf_token`, `license`, `is_builtin`, `supported_engines` (JSON).
`seed_catalog` (`workers_svc.py:279-297`) делает upsert по `name` и **всегда** ставит
`is_builtin=True` — значит кастомная модель, названная как builtin, будет перезаписвана на старте
сервера, и такие имена надо отклонять при создании. Деплой уже валидирует движок по модели:
`api/instances.py:53` — `if body.engine not in (model.supported_engines or [])` → 400.

Решения этого плана:

- **`supported_engines` выводится из категории на сервере**, а не вводится пользователем:
  {`llm`: [vllm, sglang], `embedding`: [vllm, sglang], `multimodal`: [vllm, sglang],
  `reranker`: [sglang], `audio`: []} — ровно так размечен `fixtures/catalog.json` (проверено:
  llm/embedding/multimodal → vllm+sglang, reranker → sglang, audio → пусто) и согласовано с
  `docs/engine-matrix.md` (reranker-vllm и audio — unsupported). `audio`-модель создаётся, но
  деплой любой движок отклонит 400 — это ожидаемое поведение (роадмап: «category → engine»
  валидируется на деплое).
- **Автоопределение VRAM через HF API не делаем** — роадмап называет его опциональным и
  ненадёжным; `vram_required_mb` вводится руками.
- **Удаление включаем** (роадмап держит его в Decisions): только custom и только без деплойментов.
  Иначе кастомные модели накапливаются неудаляемо.

## Prerequisites

- `main` после волны 0; `p04-instance-logs` влит (или как минимум `main` актуален) — этот план не
  трогает `proto` и `alembic`, поэтому жёсткой зависимости по ревизиям нет, но порядок волны A
  сохраняется: 04 → 03 → 05.
- `cd server && uv sync --dev` и `uv run pytest` зелёные; `cd frontend && npm ci` выполнен.

## Общие правила

- Наблюдения — в `evidence-p03-custom-models.md` рядом с планом (в `docs/roadmap/plans/`, коммитится); иных файлов наблюдений не создаётся.
- REST-схемы меняются → `cd server && uv run python ../scripts/export-openapi.py` и коммит
  `docs/openapi.yaml` (CI-джоб `openapi-check`).
- Фронт: React Query через `src/api/hooks.ts`, никаких прямых `fetch`; паттерн admin-гейта — как в
  `frontend/src/pages/UsersPage.tsx:30-34` (`user?.role !== "admin"` → сообщение вместо формы).
- Никаких новых зависимостей (frontend и server) — форма и бейджи уже покрываются текущим стеком.

## Approach

### Шаг 1 — схемы и серверный эндпоинт создания

`server/inferna_server/schemas.py`:

```python
class ModelCreate(BaseModel):
    hf_repo_id: str = Field(min_length=3, max_length=255)   # "Qwen/Qwen2.5-3B-Instruct"
    display_name: str | None = Field(default=None, max_length=255)
    category: Literal["llm", "embedding", "reranker", "audio", "multimodal"]
    vram_required_mb: int = Field(ge=256, le=1_000_000)
    requires_hf_token: bool = False
    license: str | None = Field(default=None, max_length=64)
    description: str | None = None
```

`server/inferna_server/api/models.py` — модульная карта и обработчик:

```python
CATEGORY_ENGINES: dict[str, list[str]] = {
    "llm": ["vllm", "sglang"], "embedding": ["vllm", "sglang"], "multimodal": ["vllm", "sglang"],
    "reranker": ["sglang"], "audio": [],
}

@router.post("", response_model=ModelOut, status_code=201)
async def create_model(body: ModelCreate, _: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)) -> Model:
```

Правила: `display_name = body.display_name or body.hf_repo_id`; `is_builtin=False`; `params_b=None`;
`supported_engines = CATEGORY_ENGINES[body.category]`; `name = body.hf_repo_id.strip()`.
Конфликты: существующая строка с тем же `name` → 409 `"model already exists"`; если найденная
строка `is_builtin` → 409 `"name is reserved by a builtin model"` (проверять `is_builtin`
первым). `require_admin` импортируется из `inferna_server.auth` (`auth.py:72-76`) — 403
`"Admin privileges required"` для не-админа (глобальный формат деталей в схеме остаётся как есть,
это осознанное исключение: сообщение уже такое в проекте).

`DELETE /api/v1/models/{model_id}` (admin): 404 `"model not found"`; 409
`"builtin models cannot be deleted"` при `is_builtin`; 409 `"model has deployments"` если есть
`Deployment` с этим `model_id` (достаточно факта наличия строки деплоймента);
иначе `db.delete` + 204.

Проверка шага:
- `cd server && uv run ruff check . && uv run pyright && uv run pytest`.
- Новые тесты `server/tests/test_custom_models.py` (docstring «Custom model catalog: create/delete
  rules»), фикстуры из `conftest.py` (`login`, `auth_headers`, `db`):
  `test_create_custom_model_returns_201_with_derived_engines` (llm → [vllm, sglang],
  `is_builtin is False`), `test_create_embedding_and_reranker_engines` (reranker → [sglang]),
  `test_create_rejects_builtin_name` (name из `fixtures/catalog.json` → 409),
  `test_create_rejects_duplicate_custom_name` (409),
  `test_create_forbidden_for_user_role` (403),
  `test_create_invalid_category_and_vram_rejected` (422),
  `test_delete_custom_model` (204 + 404 после), `test_delete_builtin_rejected` (409),
  `test_delete_with_deployment_rejected` (409; деплоймент через `deployment_factory`).
- `cd server && uv run python ../scripts/export-openapi.py --check` (после регенерации).

### Шаг 2 — деплой кастомной модели (контрактный тест, код не меняется)

`api/instances.py` уже читает `model.supported_engines`; проверяем, что путь работает для
кастомной строки:

`server/tests/test_custom_models.py::test_custom_model_deploys_and_respects_engine_gate`:
создать модель (llm, `vram_required_mb=2048`), `add_connected_worker(db, cluster_id)`, POST
`/api/v1/model-instances` с `engine="vllm"`, `gpu_selection="auto"` → 201 и `state="scheduled"`;
затем создать `audio`-модель и POST с `engine="vllm"` → 400 (сообщение из `instances.py:53`).
Никаких правок в `instances.py` — если тест падает, это дефект прошлых фаз, фиксировать в evidence,
а не «чинить» здесь расширением объёма.

### Шаг 3 — фронт: форма создания и метка custom

- `frontend/src/api/types.ts` — `ModelInfo` (`:50-62`) уже содержит `is_builtin`, `description`,
  `license`, `supported_engines` — правок не требуется; отдельный тип для тела POST не нужен
  (поля описываются инлайн в мутации, как в `useCreateUser`).
- `frontend/src/api/hooks.ts` — `useCreateModel()`:
  `useMutation({ mutationFn: (body) => api<ModelInfo>("/models", { method: "POST",
  body: JSON.stringify(body) }), onSuccess: () => queryClient.invalidateQueries({queryKey: ["models"]}) })`
  и `useDeleteModel()` (`DELETE /models/{id}`, инвалидация `["models"]`).
- `frontend/src/pages/ModelsPage.tsx`:
  - для админа — кнопка `Add model` и форма (поля: repo id, display name, category select
    (5 значений), VRAM MB, license, description, чекбокс «Requires HF token»); submit через
    `mutateAsync` с `try/catch` и показом `err.message` (паттерн `UsersPage.tsx:37-50`);
  - карточка: бейдж `custom` для `!model.is_builtin` (рядом с бейджем категории) и кнопка
    `Delete` для custom (через `ConfirmDialog`, паттерн `UsersPage`/`InstancesPage`);
  - форма и удаление скрыты для не-админа (как `UsersPage.tsx:30-34`).
- Юнит-тест `frontend/src/__tests__/ModelsPage.test.tsx` (stub fetch, `QueryClientProvider`):
  форма отправляет POST с ожидаемым телом; custom-карточка показывает бейдж `custom`.
- e2e `frontend/e2e/custom-model.spec.ts` (образец `frontend/e2e/deploy.spec.ts`): логин админом →
  Models → `Add model` (repo id `Qwen/Qwen2.5-0.5B-Instruct-custom`, category llm, VRAM 2048) →
  карточка появилась с бейджем `custom` → Deploy → `/instances` → строка дошла до `running` →
  Delete модели после удаления инстанса (в e2e чистить деплойменты через API, как в
  `deploy-replicas.spec.ts:5-22`).

Проверка шага: `cd frontend && npm run lint && npx tsc --noEmit && npx vitest run`, затем стек
mock (`docker compose -f docker-compose.yml -f docker-compose.mock.yml up -d --build postgres server worker`)
→ `npx playwright test e2e/custom-model.spec.ts`.

### Шаг 4 — ручная проверка приёмки

Стек mock + `bash scripts/parity.sh`-независимо: через UI создать `Qwen/Qwen2.5-3B-Instruct`
(vram 8192, category llm, gated off) → Deploy (auto) → дождаться `running` → Stop → Delete
инстанса → Delete модели. Записать в evidence: скриншот/строки UI, ответы API.

## Verification (сводно)

1. `cd server && uv run ruff check . && uv run pyright && uv run pytest`.
2. `cd server && uv run python ../scripts/export-openapi.py --check`.
3. `cd frontend && npm run lint && npx tsc --noEmit && npx vitest run`.
4. e2e: стек mock up → `npx playwright test e2e/custom-model.spec.ts`.
5. Ручной сценарий шага 4 (приёмка `03`: произвольный HF-репозиторий без правок кода, метка
   custom, deploy/stop как у builtin).

## Assumptions & contingencies

- **Уникальность имени в БД (`Model.name unique`) — единственный барьер**; расовые гонки двух
  одновременных POST обрабатываются `IntegrityError` → 409 `"model already exists"` (обернуть
  commit в `try/except IntegrityError: await db.rollback()` по образцу
  `api/deployments.py:58-62`).
- **`seed_catalog` на старте**: кастомная модель с уникальным именем не трогается (upsert идёт по
  именам фикстуры); тест на это уже есть по смыслу в `03`-приёмке — добавить
  `test_seed_catalog_does_not_touch_custom_models` в `test_custom_models.py` (создать модель, вызвать
  `seed_catalog(db)`, проверить `is_builtin is False` и `vram_required_mb` не изменился).
- **`audio`-модель создаётся с пустым `supported_engines`** → любой деплой вернёт 400; если роадмап
  позже потребует другое поведение (скрывать audio в UI), это отдельная задача.
- **Удаление с деплойментами** запрещаем по факту наличия строки `Deployment` (даже если все
  инстансы остановлены) — консервативно; роадмап говорит «no live instances», но каскад
  `Deployment.instances` при удалении модели унесёт историю остановленных инстансов. Если это
  окажется мешающим, мягчить до «нет live instances» отдельным решением, зафиксировав в evidence.
- VRAM вводится руками; ошибка в VRAM проявляется как 400 от планировщика при деплое — это
  ожидаемый путь, описанный в роадмапе.

## Evidence Artifacts

`evidence-p03-custom-models.md` рядом с планом (в `docs/roadmap/plans/`, коммитится): шаг → команда → сокращённый вывод → результат →
дата. Прогонные артефакты e2e (test-results) не коммитятся (уже покрыты `.gitignore`).