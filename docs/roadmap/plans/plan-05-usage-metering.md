# План: Usage Metering (README этап 5, `docs/roadmap/05-usage-metering.md`)

Слаг: `p05-usage-metering`. Ветка: `p05-usage-metering` от `main` (волна A, третья: после
`p04-instance-logs`, `p03-custom-models`).

## Context

Истории потребления нет: `UsageRecord` не существует (grep даёт только доки), `services/usage.py` и
`api/usage.py` отсутствуют, `/api/v1/usage` не зарегистрирован, per-user/per-key метрик нет.
Единственный сбор — `_record_usage` (`api/gateway.py:495-506`), который регуляркой по последним
8 чанкам инкрементирует `inferna_tokens{model,kind}`; никакой персистентности.

Три факта из кода, определяющие объём (проверено чтением, не по докам):

1. **Нет точки, где одновременно доступны `(user_id, model_id, tokens, latency)`**.
   `user_id`/`key` живёт только в обработчиках (`gateway.py:750-756`), `model_id` — только в
   `_proxy` (`:555`), токены — только в `_relay` (`:521`), общая latency не измеряется вовсе
   (`started` на `:601` уходит лишь в TTFB). Значит `key` придётся протащить сквозь
   `_proxy` → `_relay` (см. шаг 2).
2. **Сегодняшний учёт токенов доказуемо теряется**: `yield chunk` (`:519`) внутри `try`, при
   отключении клиента встаёт `GeneratorExit`, `except (httpx.TransportError, httpx.DecodingError)`
   (`:520`) его не ловит → `_record_usage` (`:521`) не выполняется; при обрыве upstream — то же.
   Существующий тест `test_client_disconnect_closes_upstream` проверяет только закрытие upstream.
   Поэтому запись usage обязана быть в `finally`, иначе новая таблица унаследует ту же дыру.
3. **Стриминговые клиенты без `stream_options.include_usage` не получают usage-чанк** (grep
   `stream_options|include_usage` по `server/` пуст) → по приёмке такие запросы считаются только по
   числу запросов. Чтобы токены всё же были, план инжектит `stream_options.include_usage` в тело
   (шаг 2, отдельный флаг конфига).

Решения этого плана:

- **Одна таблица агрегатов + in-memory буфер**, без таблицы сырых событий: gateway накапливает
  агрегат в памяти (ключ `(user_id, api_key_id, model_id, period_start)`), периодический флушер
  делает upsert в `usage_records` — ровно паттерн `_last_used_dirty` +
  `flush_last_used_stamps` (`gateway.py:79-80,174-197`, `main.py:94-100`). Сырые события не нужны
  приёмке (нужны сутки/час), а таблица событий росла бы неограниченно.
- **`gpu_hours` — отдельная таблица `gpu_usage_records`** (`model_id, cluster_id, period_start,
  gpu_seconds`), заполняемая сэмплером занятости. Причина: у `Deployment` нет владельца
  (`models.py:165-204`: только `model_id/cluster_id/engine/profile/min/max/created_at/last_scaled_at`),
  поэтому приписать GPU-часы пользователю нечем; модель+кластер — минимальная честная атрибуция.
  В API `gpu_hours = gpu_seconds / 3600`.
- **Квоты включены** (вторая половина плана): `05` называет их опциональными, но `06` M8 держит
  их в той же ветке `w4/usage-metering`, и M8-гейт без них не закрывается.
- **Без новых фронтенд-зависимостей**: графа/графиков в стеке нет (только `VramBar.tsx`), поэтому
  Usage-страница — таблица + полосы по образцу `VramBar`.

## Prerequisites

- `main` после волны 0; `p04` влит (для ревизии: этот план — **третья** ревизия волны A,
  `down_revision` = head после `p04`).
- `cd server && uv sync --dev`, `uv run pytest` зелёные; `cd frontend && npm ci`.
- `docker compose -f docker-compose.yml -f docker-compose.mock.yml` поднимается; gateway включён
  (`INFERNA_GATEWAY_ENABLED=true` в compose, `docker-compose.yml:29`).

## Общие правила

- Наблюдения — в `evidence-p05-usage-metering.md` рядом с планом (в `docs/roadmap/plans/`, коммитится).
- Новые env — префикс `INFERNA_`, задокументировать в `.env.example` и `server/config.py`.
- Новые метрики — **вне** `_ALL_GAUGES` (`services/metrics.py:51-58`), иначе `refresh_gauges`
  будет их обнулять.
- После правки REST-схем — `export-openapi.py` и коммит `docs/openapi.yaml`.
- Гейт «данные сверяются с Prometheus» реализуется скриптом (шаг 6), а не глазами.

## Approach

### Шаг 1 — модель данных и миграция

`server/inferna_server/models.py`:

```python
class UsageRecord(Base, TimestampMixin):
    __tablename__ = "usage_records"
    __table_args__ = (UniqueConstraint("user_id", "api_key_id", "model_id", "period_start",
                                       name="uq_usage_records_period"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"), index=True)
    period_start: Mapped[date] = mapped_column(Date, index=True)     # UTC-сутки
    requests: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms_sum: Mapped[int] = mapped_column(Integer, default=0)  # avg = sum / requests

class GpuUsageRecord(Base, TimestampMixin):
    __tablename__ = "gpu_usage_records"
    __table_args__ = (UniqueConstraint("model_id", "cluster_id", "period_start",
                                       name="uq_gpu_usage_period"),)
    id, model_id (FK CASCADE), cluster_id (FK CASCADE), period_start (Date),
    gpu_seconds: Mapped[float]
```

Миграция: `cd server && uv run alembic revision -m "usage records"`; `down_revision` = текущий head;
`upgrade()` — два `op.create_table` + индексы по образцу
`alembic/versions/d18580a27109_phase1_gateway.py:49-70` (FK с `ondelete='CASCADE'`), для SQLite-веток
`batch_alter_table` не нужен (новые таблицы создаются напрямую), `downgrade()` — `drop_table` в
обратном порядке.

Проверка: `bash scripts/check-migrations.sh` (Postgres) + `cd server && uv run pytest`
(тестовая БД создаётся `Base.metadata.create_all`, новые таблицы появляются автоматически).

### Шаг 2 — запись события из gateway (и починка потери)

`server/inferna_server/api/gateway.py`:

1. `@dataclass(slots=True) class UsageContext: user_id: uuid.UUID; api_key_id: uuid.UUID;
   model_id: uuid.UUID; instance_id: uuid.UUID; model_name: str; started: float`.
2. Обработчики (`chat_completions:750`, `embeddings:765`, `audio_transcriptions:780`) передают
   `key` в `_proxy(request, raw_body, model_name, db, key=key)`.
3. `_proxy` (`:540-618`) собирает `ctx` (там же, где известны `model`/`instance`, `:555`) и передаёт
   его в `_relay(resp, model_name, path, ctx)`; **все** ветки выхода из `_proxy` вызывают
   `services/usage.record_request_outcome(ctx, outcome=...)`:
   - успешный ответ (`:605-616`) — outcome определяется в `_relay` при завершении;
   - `except (httpx.TransportError, httpx.InvalidURL)` (`:587-599`) и SSRF-блок (`:433-435`) —
     `outcome="upstream_error"`, токены 0;
   - `OpenAIError` до `_resolve_target` (404/401/413) — не пишем (нет `model_id`); это согласуется
     с существующим `inferna_requests{model="unknown"}`.
4. `_relay` (`:509-525`) — учёт переносится в `finally`, чтобы `GeneratorExit` (отключение клиента)
   и обрыв upstream больше ничего не теряли:
   ```python
   finally:
       usage_ctx.tokens = parsed_tokens(tail) or None      # None = usage-чанка не было
       usage_ctx.e2e_ms = (time.monotonic() - usage_ctx.started) * 1000
       record_request(usage_ctx)                            # буфер в services/usage.py
       await resp.aclose()
   ```
   `_record_usage` (`:495-506`) остаётся (инкремент Prometheus-счётчиков) и вызывается там же.
   Дополнительно инкрементируется новый счётчик `inferna_usage_events_total{model,outcome}` с
   `outcome ∈ {ok, no_usage_chunk, upstream_error, client_disconnect}` (`client_disconnect`
   определяется по `GeneratorExit`/отсутствию `[DONE]`).
5. Инжекция usage для стримов: перед `client.send` (`:592`), если
   `settings.gateway_inject_stream_usage` и тело — JSON с `"stream": true` и без
   `stream_options`, добавить `"stream_options": {"include_usage": true}`. Новая настройка
   `gateway_inject_stream_usage: bool = True` (`INFERNA_GATEWAY_INJECT_STREAM_USAGE`). Оговорка:
   это меняет байты, отправляемые upstream; клиентам возвращается дополнительный usage-чанк
   (стандартное поведение OpenAI-совместимых серверов), приёмка «стримы без usage-чанка считаются
   по запросам» сохраняется как fallback при выключенном флаге.

Проверка шага:
- `cd server && uv run pytest` + новые тесты в `server/tests/test_usage.py`:
  `test_streaming_request_records_tokens` (MockTransport с `SSE_BODY`, `:26-31`), 
  `test_client_disconnect_still_records_request` — **регресс на пункт 2 Context**
  (повторить `test_gateway.py:776-828`, но утверждать наличие строки/буфера с `requests == 1`),
  `test_upstream_502_records_request_without_tokens`,
  `test_no_usage_chunk_records_request_only` (SSE без usage-чанка),
  `test_inject_stream_usage_adds_stream_options` (captured body из MockTransport содержит
  `stream_options.include_usage`).
- Реестр метрик: `REGISTRY.get_sample_value("inferna_usage_events_total", {...})` до/после.

### Шаг 3 — агрегатор и сэмплер занятости

Новый `server/inferna_server/services/usage.py` (паттерн `services/workers_svc.mark_disconnected`
+ `flush_last_used_stamps`):

- `_PENDING: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID, date], _Accum]` + `_PENDING_LOCK`
  (`asyncio.Lock`; инвариант «нет await внутри lock» — как в `gateway._active_lock`).
- `def record_request(ctx, tokens_in, tokens_out, latency_ms, outcome)` — синхронный, вызывается из
  gateway; заводит/дополняет агрегат (requests += 1 даже при `no_usage_chunk`/`upstream_error`).
  Буфер ограничен (`MAX_PENDING = 10_000` ключей): при переполнении — сбросить самый старый агрегат
  в `logger.warning("usage buffer overflow")` и посчитать потерю счётчиком.
- `async def flush_usage(db: AsyncSession) -> int` — забрать снапшот `_PENDING`, для каждого ключа
  `select(UsageRecord).where(...)` → `update` или `insert`, один `db.commit()`; при исключении
  вернуть агрегаты обратно в буфер (как `test_flush_requeues_stamps_on_failure` для last-used).
- `async def sample_occupancy(db, interval_seconds: float)` — для каждого инстанса в
  `LIVE_STATES` (`models.py:26`): `gpu_seconds += interval_seconds * len(gpu_indexes)`, upsert в
  `GpuUsageRecord` по `(model_id, cluster_id, period_start)`.
- `async def usage_flush_loop()` — `while True:` → `async with SessionLocal() as db:` →
  `flush_usage(db)` → `sample_occupancy(db, USAGE_FLUSH_INTERVAL_SECONDS)` → `asyncio.sleep(...)`
  с `try/except Exception: logger.exception(...)` (как `last_used_flush_loop`, `main.py:94-100`).
  Константа `USAGE_FLUSH_INTERVAL_SECONDS = 30` (модульная; при необходимости — настройкой, но
  стартовая версия — константа, чтобы не плодить env).

`server/inferna_server/main.py` — четвёртая задача в lifespan рядом с существующими тремя
(`:110-115`): `usage_task = asyncio.create_task(usage_flush_loop())`, `app.state.usage_task = usage_task`,
и добавить её в кортеж отмены на shutdown (`:130-140`) + финальный best-effort `flush_usage` перед
закрытием (по образцу финального `flush_last_used_stamps`).

Проверка шага: `server/tests/test_usage.py`:
`test_flush_upserts_and_is_idempotent` (двойной флуш не дублирует requests),
`test_flush_requeues_on_failure` (подменить сессию стаб-сессией, паттерн
`test_gateway.py:910-935`), `test_occupancy_sampler_adds_gpu_seconds`
(два живых инстанса с 1 GPU, interval 30 → 60 gpu_seconds),
`test_flush_loop_is_cancellable` (запустить задачу, отменить, убедиться в выходе).

### Шаг 4 — REST: `GET /api/v1/usage`

Новый `server/inferna_server/api/usage.py` по образцу `api/dashboard.py` (один обработчик,
`get_current_user`, без `require_admin` роутера) + фильтр роли как в `api/keys.py:49-56`:

```
GET /api/v1/usage?days=7&model_id=<uuid>&key_id=<uuid>
→ UsageOut{
    totals: {requests, tokens_in, tokens_out, avg_latency_ms, gpu_hours},
    rows: [{period_start, model_id, model_name, user_id?, username?, api_key_id?,
            requests, tokens_in, tokens_out, avg_latency_ms, gpu_hours}],
    limits?: {token_limit, request_limit, tokens_used, requests_used}   # только для своей учётки
  }
```

- `WHERE` строится условно: `role == "admin"` → без фильтра по пользователю; иначе
  `UsageRecord.user_id == current.id`. `key_id` не-админом принимается только свой (иначе 403, как
  `api/keys.py:78-79`).
- `days` ограничен `1..90`; агрегация по `period_start` (сутки). `gpu_hours` присоединяется из
  `GpuUsageRecord` по `(model_id, period_start)` (для не-админского фильтра — без разбивки по
  пользователю, это осознанное ограничение: GPU-часы не атрибутируются пользователю, §Context).
- Регистрация в `api/__init__.py:34-45`; `cd server && uv run python ../scripts/export-openapi.py`.

Проверка: `server/tests/test_usage.py::test_usage_admin_sees_all_users`,
`::test_usage_user_sees_only_own` (форма теста `test_keys.py:52-72`),
`::test_usage_forbids_foreign_key_filter` (403), `::test_usage_days_bounds` (422 на 0/91),
`::test_usage_totals_include_gpu_hours`; плюс `export-openapi.py --check`.

### Шаг 5 — квоты (вторая половина)

- `models.py:User` — `token_limit: Mapped[int | None]`, `request_limit: Mapped[int | None]`
  (nullable, без server_default), одна ревизия поверх шага 1 (отдельный `alembic revision -m
  "user usage quotas"`).
- `server/inferna_server/api/users.py` — `PUT /api/v1/users/{user_id}/limits` (`require_admin`),
  тело `{token_limit: int | None, request_limit: int | None}` (оба `ge=0`, `None` = без лимита);
  `UserOut` получает эти поля.
- Gateway: перед отправкой запроса (после `_resolve_target`, до `client.send`) — проверка лимитов:
  used = сумма `UsageRecord` за текущие UTC-сутки + незакрытый буфер; при превышении → 429 в
  OpenAI-форме (`openai_error(...)` с `code="quota_exceeded"`, `model="unknown"`) и
  `record_request_outcome(outcome="quota_rejected")` без инкремента токенов. Проверка выносится в
  `services/usage.py:async def check_quota(db, user) -> None | tuple[int, int]` (тестируемо без HTTP).
- UI: `UsersPage` (админ) — поля лимитов в форме создания и колонка в таблице; `UsagePage` — блок
  «remaining» для своей учётки (из `UsageOut.limits`).

Проверка: `::test_quota_rejects_after_token_limit` (429 + `code=quota_exceeded` + счётчик
`quota_rejected`), `::test_quota_disabled_when_null`, `server/tests/test_users.py` — новые тесты на
403/422 для `/limits`.

### Шаг 6 — фронт, Grafana, сверка

- `frontend/src/api/types.ts` — `UsageRow`, `UsageOut` (snake_case, зеркалят `UsageOut`).
- `frontend/src/api/hooks.ts` — `useUsage(params)`: `useQuery({queryKey: ["usage", params],
  queryFn: () => api<UsageOut>(`/usage?${qs}`), refetchInterval: LIVE_INTERVAL})`.
- `frontend/src/pages/UsagePage.tsx` — таблица (период, модель, запросы, токены in/out, avg
  latency, GPU-часы) + полосы по образцу `components/VramBar.tsx` (доля токенов относительно
  максимума в выборке), пустое состояние как в `DashboardPage.tsx:57-63`; у админа — колонка
  user/key.
- `frontend/src/App.tsx:34-45` — `<Route path="/usage" element={<UsagePage />} />`;
  `frontend/src/components/Sidebar.tsx:4-10` — пункт `{to: "/usage", label: "Usage"}` (для всех).
- Grafana: `deploy/grafana/dashboards/gateway.json` — добавить два панели на
  `inferna_usage_events_total` (`sum by (outcome) (rate(...))`) и сверку
  `sum(rate(inferna_usage_events_total[5m])) / sum(rate(inferna_requests_total[5m]))`
  (панель «usage events / gateway requests»); uid дашборда не меняется.
- `scripts/usage-reconcile.sh` (новый, curl+jq): снять дельту `inferna_requests_total` и
  `inferna_tokens_total` из `/metrics` за окно, сравнить с суммой из `GET /api/v1/usage`
  (`days=1`) — печатать таблицу и падать при расхождении > 1 % (порог — параметр скрипта
  `RECONCILE_TOLERANCE_PCT`, по умолчанию 1). Это исполняемый вид гейта M8
  «usage-агрегаты сверяются с gateway-метриками».

Проверка шага 6: `cd frontend && npm run lint && npx tsc --noEmit && npx vitest run` + новый
`frontend/src/__tests__/UsagePage.test.tsx` (рендер строк, пустое состояние);
e2e `frontend/e2e/usage.spec.ts`: логин → создать API-ключ (паттерн `e2e/keys.spec.ts`) → один
запрос к `/v1/chat/completions` (в mock-стенде upstream отсутствует → 502, но запрос учитывается)
→ `/usage` показывает строку с `requests ≥ 1` → `bash scripts/usage-reconcile.sh` печатает таблицу
и не падает (расхождение 0).

## Verification (сводно)

1. `cd server && uv run ruff check . && uv run pyright && uv run pytest`.
2. `bash scripts/check-migrations.sh` (Postgres) + `uv run alembic upgrade head`.
3. `cd server && uv run python ../scripts/export-openapi.py --check`.
4. `cd frontend && npm run lint && npx tsc --noEmit && npx vitest run`.
5. Стек mock: `docker compose -f docker-compose.yml -f docker-compose.mock.yml up -d --build
   postgres server worker` → e2e `npx playwright test e2e/usage.spec.ts`.
6. Приёмка `05`: после gateway-запросов `GET /api/v1/usage` показывает per-user/model/day;
   `bash scripts/usage-reconcile.sh` — расхождение в пределах допуска; стрим без usage-чанка
   (`INFERNA_GATEWAY_INJECT_STREAM_USAGE=false`) считается по запросам.

## Assumptions & contingencies

- **Потеря на рестарте**: буфер in-memory, при падении сервера недописанные агрегаты теряются
  (≤ 30 с трафика). Это тот же класс допущения, что «Prometheus-счётчики сбрасываются на
  рестарте»; если потребуется строгая гарантия — добавить таблицу сырых событий и писать туда из
  `finally` (изменение объёма, требует отдельного решения).
- **`avg_latency_ms` = `latency_ms_sum / requests`** по суткам: точная гистограмма не хранится.
  Это соответствует колонке роадмапа (`avg_latency_ms`).
- **Инжекция `stream_options`** меняет тело запроса к upstream. Если это окажется неприемлемым для
  какого-то движка (SGLang/vLLM без поддержки), флаг отключается, и учёт деградирует до «по
  запросам» — приёмка это допускает.
- **GPU-часы не атрибутируются пользователю** — следствие отсутствия владельца у `Deployment`;
  если в M8 понадобится per-user экономика, потребуется колонка владельца у `Deployment` (отдельная
  задача, не расширять здесь).
- **Квоты «мягкие»**: проверка по текущим UTC-суткам, без резервирования; при всплеске под лимитом
  допускается превышение на число параллельных запросов. Порог `ge=0`, `None` = без лимита.
- Порядок ревизий: этот план — последний писатель `models.py`/`alembic` в волне A; после него
  `models.py` переходит `w0/multinode-contract` (волна C).

## Evidence Artifacts

`evidence-p05-usage-metering.md` рядом с планом (в `docs/roadmap/plans/`, коммитится): шаг → команда → сокращённый вывод → результат →
дата. Артефакты сверки (`artifacts/usage-reconcile-*.jsonl` или вывод скрипта) — git-ignored
(`artifacts/` добавляется в `.gitignore` волной 0 программы).