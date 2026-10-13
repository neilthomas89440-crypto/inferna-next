# План: Instance Logs в UI (README этап 2, `docs/roadmap/04-instance-logs.md`)

Слаг: `p04-instance-logs`. Ветка: `p04-instance-logs` от `main` (после волны 0 программы).
Рабочая директория: `C:/Users/admin/Desktop/projects/inferna-next`.

## Context

Сейчас причина падения инстанса видна только как `ModelInstance.error_detail` (`Text`,
`models.py:253`), которая уходит в UI как tooltip (`InstancesPage.tsx:159-166`). Воркер уже умеет
читать лог-хвост (`_container_log_tail`, `engines/manager.py:450-458`, `LOG_TAIL_LINES=20`) и уже
кладёт его в `detail` при ошибке health-probe (`manager.py:407-409`, `:446-448`). Нужен снапшот
хвоста для live/error инстансов и его показ в UI. Поток данных целиком существует для `detail`:
`statuses()` → `InstanceStatus` → `sync_worker` (`workers_svc.py:214-221`) → REST → React Query.
Задача — добавить в этот поток поле `log_tail` на всех шести слоях (proto, воркер, БД, API, hook,
панель) и один новый эндпоинт.

Решения, принятые в этом плане (роадмап допускает оба варианта хранения):

- **Хранение — колонка БД `ModelInstance.log_tail`**, не in-memory: рестарт сервера не должен
  стирать диагностику, и это тот же паттерн, что `error_detail` (один commit в `sync_worker`).
  Цена — `Text` 8 KiB на инстанс, перезапись раз в 5 с.
- **Стриминг логов (SSE/gRPC stream) не делаем** — роадмап прямо выводит его за скоуп этапа;
  автообновление идёт по существующему polling `LIVE_INTERVAL = 5000` (`hooks.ts:19`).
- **Отдельный эндпоинт, а не поле в `InstanceOut`**: логи не должны ехать в каждом ответе списка
  инстансов.

## Prerequisites

- Ветка `p04-instance-logs` от актуального `main` (волна 0 программы: `phase2` влита).
- `cd server && uv sync --dev`, `cd worker && uv sync --dev`, `uv run pytest` в обеих — зелёные.
- Alembic head на момент старта — `b4d7e91c2a58`; если он изменился, `down_revision` брать
  фактический (проверить `uv run alembic heads`).

## Общие правила

- Шаг завершается полным набором своих проверок; проверки не переносятся в конец.
- Все наблюдения пишутся в `evidence-p04-instance-logs.md` (в папке плана); иных файлов наблюдений
  не создаётся.
- Python 3.12/uv, ruff + pyright, `structlog`; фронт — React Query через `hooks.ts`, без `fetch`.
- После правки proto — `bash scripts/gen-proto.sh` и коммит стабов; после правки схем REST —
  `cd server && uv run python ../scripts/export-openapi.py` и коммит `docs/openapi.yaml`.
- Ничего из 06 (parallelism/rendezvous) в этом плане не трогается.

## Approach

### Шаг 1 — proto: `log_tail` в `InstanceStatus`

Файл: `proto/cluster.proto`. Добавить в `message InstanceStatus` (строки 34-41) поле
`string log_tail = 6;  // last N container log lines; empty when unavailable`.

Проверка шага:
`bash scripts/gen-proto.sh && git diff --exit-code -- proto server/inferna_server/proto
worker/inferna_worker/proto` — после коммита стабов расхождений нет; `cd server && uv run pytest`
и `cd worker && uv run pytest` зелёные (поле аддитивное, парсеры его игнорируют).

### Шаг 2 — воркер: сбор хвоста в `statuses()`

Файл: `worker/inferna_worker/engines/manager.py`.

- `statuses()` (сейчас `:460-478`, синхронный) → `async def statuses(self) -> list[InstanceStatus]`:
  для каждого инстанса в состоянии `starting | running | error` читать хвост
  `await asyncio.to_thread(self._container_log_tail, instance_id)`; результат складывать в
  `info["log_tail"]`, и **не перезаписывать непустой кэш пустой строкой** (после `stopped`
  хвост остаётся последним известным). Для `mock` — синтезировать детерминированные строки
  (см. ниже), реальный docker-хвост недоступен.
- Mock синтез (требование приёмки «mock mode synthesizes logs»): в `_start_mock` (`:218-238`) и в
  переходе `starting → running` (`:466-468`) заполнять `info["log_tail"]` строками
  `["mock: pulling image <image>", "mock: loading model <model_name>", "mock: engine ready on port <port>"]`
  (для `error` — оставить `detail` как есть и добавить строку `mock: error — <detail>`).
- `worker/inferna_worker/main.py:109` — `instances=await manager.statuses()`.
- Все синхронные вызовы обновляются в том же коммите: mock-ветка `reconcile()` (`manager.py:139-150`)
  → `return await self.statuses()`; `worker/tests/test_manager_mock.py:60,68,76,245` → `await`.

Проверка шага:
- `cd worker && uv run ruff check . && uv run pyright && uv run pytest`.
- Новые тесты: `worker/tests/test_manager_mock.py::test_mock_statuses_carry_log_tail`
  (mock: `log_tail` непустой у `running`), и новый `worker/tests/test_manager_logs.py` с
  fake-docker по образцу `worker/tests/test_manager_mock.py` (`FakeContainer.logs(tail=20) ->
  b"fake log"` уже есть): `test_real_statuses_read_log_tail_in_thread` (хвост доехал),
  `test_log_tail_survives_stop` (после stop непустой хвост не затёрт).
- Отдельно зафиксировать, что Sync-цикл не блокируется: тест с fake-docker, чей `logs()`
  спит 0.5 с, укладывается в таймаут (`asyncio.wait_for(manager.statuses(), 2)`).

### Шаг 3 — сервер: хранение снапшота

- `server/inferna_server/models.py` — в `ModelInstance` (после `error_detail`, `:253`) добавить
  `log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)`.
- Миграция: `cd server && uv run alembic revision -m "instance log tail"` (шаблон
  `server/alembic/script.py.mako`, `file_template = %%(rev)s_%%(slug)s`), `down_revision` =
  текущий head, `upgrade()` — `op.add_column("model_instances", sa.Column("log_tail", sa.Text(),
  nullable=True))` с SQLite-веткой `batch_alter_table(..., recreate="always")` (образец:
  `alembic/versions/b4d7e91c2a58_deployment_max_replicas_upper_bound.py`), `downgrade()` — drop.
- `server/inferna_server/services/workers_svc.py:214-221` — рядом с существующим
  `inst.error_detail = status.detail or None` добавить
  `if status.log_tail: inst.log_tail = status.log_tail[:LOG_TAIL_MAX_CHARS]`, где
  `LOG_TAIL_MAX_CHARS = 8192` (модульная константа; защита от роста строки, обрезать по хвосту).
  Существующую семантику `error_detail` не менять.

Проверка шага:
- `cd server && uv run ruff check . && uv run pyright && uv run pytest`.
- `bash scripts/check-migrations.sh` (нужен Postgres; локально —
  `INFERNA_DATABASE_URL=postgresql+asyncpg://inferna:inferna@localhost:5432/inferna`).
- Новые тесты `server/tests/test_instance_logs.py` (docstring «Instance log snapshots: sync →
  API»): `test_sync_persists_log_tail` (Sync с `log_tail` пишет колонку),
  `test_sync_keeps_previous_tail_when_worker_reports_empty`,
  `test_sync_truncates_oversized_tail` (строка > 8192 → длина == 8192, сохранён хвост).
  Хелперы — `server/tests/conftest.py`: `add_connected_worker`, `login`, `auth_headers`;
  Sync дёргать напрямую, как в `server/tests/test_workers_sync.py`.

### Шаг 4 — REST: `GET /api/v1/model-instances/{id}/logs`

- `server/inferna_server/schemas.py` — `class InstanceLogsOut(ORMModel): instance_id: uuid.UUID;
  state: str; log_tail: str | None; updated_at: datetime`.
- `server/inferna_server/api/instances.py` — обработчик по образцу
  `@router.post("/{instance_id}/stop")` (`:112-146`): `Depends(get_current_user)`,
  `select(ModelInstance).where(id == instance_id)` (+`selectinload(worker)` не нужен),
  404 `"instance not found"`, ответ `InstanceLogsOut`.
- `cd server && uv run python ../scripts/export-openapi.py` + коммит `docs/openapi.yaml`.

Проверка шага: `cd server && uv run pytest` (новые тесты
`test_instance_logs.py::test_logs_endpoint_returns_tail`, `::test_logs_endpoint_404`,
`::test_logs_endpoint_requires_auth` (401)), плюс
`cd server && uv run python ../scripts/export-openapi.py --check`.

### Шаг 5 — фронт: панель логов

- `frontend/src/api/types.ts` — `export interface InstanceLogs { instance_id: string; state: string;
  log_tail: string | null; updated_at: string; }`.
- `frontend/src/api/hooks.ts` — `useInstanceLogs(id: string | null)`:
  `useQuery({ queryKey: ["instance-logs", id], queryFn: () => api<InstanceLogs>(`/model-instances/${id}/logs`),
  enabled: !!id, refetchInterval: LIVE_INTERVAL })`.
- `frontend/src/pages/InstancesPage.tsx` — кнопка `Logs` в строке инстанса (рядом с Stop/Retry,
  `:187-207`); состояние `logsFor: Instance | null`; модалка по существующему bespoke-паттерну
  (`endpoint`-модалка, `:264+`): заголовок с `model.display_name`, бейдж `state`, `<pre>` с
  моно-шрифтом и `whitespace-pre-wrap`, пустое состояние «No logs yet», закрытие по кнопке и
  подложке. Логи не показывать в строке таблицы (строка остаётся как есть).
- Юнит-тест: `frontend/src/__tests__/InstanceLogsModal.test.tsx` по образцу
  `__tests__/DeployDialog.test.tsx` (stub `fetch`, `QueryClientProvider` + `MemoryRouter`):
  рендер модалки с непустым `log_tail` показывает строки; с `null` — пустое состояние.

Проверка шага: `cd frontend && npm run lint && npx tsc --noEmit && npx vitest run`.

### Шаг 6 — e2e и интеграционная проверка

- `frontend/e2e/instance-logs.spec.ts` (по образцу `frontend/e2e/deploy.spec.ts:3-32`):
  логин → Models → deploy `Qwen/Qwen2.5-0.5B-Instruct` → `/instances` → дождаться `running` →
  кнопка `Logs` в строке → в модалке виден непустой mock-текст (`mock: engine ready`) →
  закрыть модалку.
- Ручная проверка (mock-стенд, шаги 4-5 программы):
  `docker compose -f docker-compose.yml -f docker-compose.mock.yml up -d --build postgres server worker`,
  deploy через UI, затем
  `curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/model-instances/$ID/logs | jq .`
  — `state == "running"`, `log_tail` непустой. Проверить, что размер ответа списка
  `/api/v1/model-instances` не вырос (логи не в `InstanceOut`) и что `updated_at` инстанса
  двигается (запись раз в Sync).

## Verification (сводно)

1. `cd server && uv run ruff check . && uv run pyright && uv run pytest`.
2. `cd worker && uv run ruff check . && uv run pyright && uv run pytest`.
3. `bash scripts/gen-proto.sh && git diff --exit-code`.
4. `cd server && uv run python ../scripts/export-openapi.py --check`.
5. Postgres: `bash scripts/check-migrations.sh`.
6. `cd frontend && npm run lint && npx tsc --noEmit && npx vitest run`.
7. e2e: стек mock up → `cd frontend && npx playwright test e2e/instance-logs.spec.ts`.

## Assumptions & contingencies

- **Хранение в БД** выбрано вместо in-memory; если окажется, что запись 8 KiB каждые 5 с мешает
  (нагрузочный тест покажет рост WAL), — переключиться на in-memory registry без изменения proto
  и API (эндпоинт читает из registry); откат дешёвый, поле proto остаётся.
- **`statuses()` становится async** — это правка сигнатуры; все вызовы (`main.py:109`, worker-тесты)
  обновляются в том же коммите. Существующие тесты, дергавшие `statuses()` синхронно, правятся,
  их смысл (mock-lifecycle) сохраняется.
- **Docker log tail для `starting`** может вернуть пусто на раннем этапе — это допустимо, UI
  показывает пустое состояние; при ошибке хвост появляется из `_health_probe`.
- **Роадмап-скоуп**: SSE/стриминг, хранение истории логов (не только последнего снапшота),
  логи рангов multi-node — вне этого плана (`06` M7 `w4/diagnostics` владеет per-rank логами).
- Если `uv run alembic heads` покажет иной head (например, после соседней ветки волны A) — взять
  фактический `down_revision`; порядок ревизий в волне A: сначала `p04-instance-logs`, затем
  `p03-custom-models` (без миграции), затем `p05-usage-metering`.

## Evidence Artifacts

Файл `evidence-p04-instance-logs.md` рядом с планом (в `docs/roadmap/plans/`, коммитится): по каждому шагу — команда, сокращённый вывод,
результат, дата. Отдельные файлы наблюдений не создаются.