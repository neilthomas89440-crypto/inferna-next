# План: Release B — autoscaling (`docs/roadmap/02-replicas-autoscaling.md`)

Слаг: `p02b-autoscaling`. Ветка: `p02b-autoscaling`. Старт — **после** волны D программы
(`06` M2.2 влит): политика должна масштабировать целые параллельные группы (`06` M8), а до M2.2
групп в модели нет. Волна E программы.

## Context

Release A доставлен: `services/scaling.py:15-17` `apply_scale(db, deployment, replicas, *,
set_range=True)` — единственный путь мутации групп, `set_range=True` пинит `min=max=replicas`
(`:105-107`); `set_range=False` — заготовка для автоскейлера (клампит в окно, `:36-41`), **но
вызовов `set_range=False` нет ни одного** (grep по repo: единственный вызов — `api/deployments.py:57`
с `set_range=True`). Фонового цикла нет: lifespan создаёт ровно три задачи
(`main.py:110-115`), в `services/` нет `autoscaling.py`. Из метрик сегодня есть только TTFB
(`inferna_time_to_first_byte_seconds`, `metrics.py:34-38`), токены (`inferna_tokens`), счётчик
запросов и gauge `inferna_instance_active_requests{instance_id,model}` (`:45-49`) — TTFT/ITL/TPS
появляются в волне B этим же роадмапом (`06` M1 `w1/gateway-telemetry`, план
`plan-06-distributed-inference.md`). Инфраструктуры replay нет: единственный трейс-артефакт —
lifecycle-`soak-*.jsonl` из `scripts/soak.sh`.

Инварианты, которые политика обязана не нарушать (проверено чтением кода):

1. **Порядок блокировок `deployment → workers`** (`scaling.py:24-34` → `scheduler.py:58-64, :106`).
   Автоскейлер не должен сначала сканировать воркеров, а потом лочить деплоймент.
2. **Атомарность портов**: `PORT_RESERVED` (`scheduler.py:16-19`) + частичный уникальный индекс
   `uq_model_instances_worker_port_active` (`models.py:217-231`) + один commit на операцию
   (`scaling.py:119`). Нельзя писать `ModelInstance` с портом в обход `allocate_replicas` и нельзя
   коммитить в середине.
3. **Anti-affinity**: в `allocate_replicas` обязан передаваться набор занятых `(worker_id, gpu_index)`
   (`scaling.py:61-66`), иначе раскладка деградирует до Pass 2.
4. **Scale-in не снимает живые реплики**: порядок `(state == "running", created_at)`
   (`scaling.py:96-104`).
5. **`ck_deployments_min_max`** (`min ≥ 1, max ≥ min, max ≤ 8`) — окно всегда валидно.
6. **desired_state/generation**: scale-in → `desired_state="stopped"` + `generation += 1`.

Дополнительная проблема, которую план обязан решить: сегодня ручной scale пинит окно
(`min_replicas = max_replicas = N`), поэтому после любого ручного действия окно автоскейлера
схлопывается, а эндпоинта «задать диапазон, не меняя число» нет.

Решения этого плана:

- **Окно задаётся отдельно**: новый `PUT /deployments/{id}/range`; ручные `Scale up/down` остаются
  в семантике Release A (pin `min=max`), диапазон и автоскейлинг — второй, явный режим в UI.
- **Cooldown живёт в памяти цикла** (`monotonic` на деплоймент), а не в `last_scaled_at`: сегодня
  `last_scaled_at` пишется на каждый вызов `apply_scale` (`:108`), включая no-op, и менять эту
  семантику — значит ломать тесты Release A. Цена: рестарт сервера сбрасывает cooldown (одна
  лишняя оценка), это документируется.
- **Шаг ±1 реплика за тик** — антиосцилляция; «scale-out не оставляет половин групп» обеспечивается
  Phase 2 (группы) и проверяется тестом.

## Prerequisites

- Волна D программы влита: есть `ModelInstance.parallel_group_id/node_rank/world_size/parallel_mode`
  и `allocate_parallel_replicas` (`06` M2.1/M2.2); `06` M1 влит: есть `inferna_ttft_seconds`,
  `inferna_inter_token_seconds`, `inferna_output_tps` (labels `model,engine,parallelism`).
- `06` M2-гейт зелёный (mock lifecycle + железо-часть записана, в т.ч. `BLOCKED:`, если железа нет).
- `cd server && uv sync --dev && uv run pytest` зелёные; Postgres доступен для concurrency-тестов.

## Общие правила

- Наблюдения — в `evidence-p02b-autoscaling.md` рядом с планом (в `docs/roadmap/plans/`, коммитится).
- Новые env — `INFERNA_*` в `.env.example` + `server/config.py`; новые метрики — вне `_ALL_GAUGES`.
- После правки REST-схем — `export-openapi.py` + коммит `docs/openapi.yaml`.
- Никаких правок `services/scheduler.py` (владелец — `w2/server-parallel-groups`); автоскейлер
  использует только публичные `allocate_replicas`/`allocate_parallel_replicas` через `apply_scale`.

## Approach

### Шаг 1 — окно диапазона

- `server/inferna_server/schemas.py` — `class DeploymentRange(BaseModel): min_replicas: int =
  Field(ge=1, le=8); max_replicas: int = Field(ge=1, le=8)` + модельный валидатор `min <= max`.
- `server/inferna_server/api/deployments.py` — `PUT /{deployment_id}/range` (auth: любой
  аутентифицированный пользователь, как у `/scale`, `:46`): загрузить с `_DEPLOYMENT_LOADS`,
  404 при отсутствии; выставить окно; привести текущее число в окно (`apply_scale(db, deployment,
  clamp(current, min, max), set_range=False)`); вернуть `DeploymentOut`; `IntegrityError` → 409
  `"allocation conflict; retry"` (образец `:58-62`). Существующий `POST /scale` не трогаем.
- `cd server && uv run python ../scripts/export-openapi.py`.

Проверка: `server/tests/test_deployments_api.py` — `test_set_range_keeps_count_inside_window`
(2 реплики, окно 1–5 → остаются 2, `min/max` = 1/5), `test_set_range_scales_out_to_min`
(окно 4–6 при 2 репликах → 4 живых), `test_set_range_scales_in_to_max` (окно 1–1 при 3 → 1),
`test_set_range_rejects_min_gt_max` (422), `test_set_range_404`, `test_set_range_requires_auth` (401).

### Шаг 2 — настройки и метрики

`server/inferna_server/config.py` (+ `.env.example`):

| env | default | смысл |
|---|---|---|
| `INFERNA_AUTOSCALE_ENABLED` | `false` | автоскейлинг выключен по умолчанию |
| `INFERNA_AUTOSCALE_INTERVAL_SECONDS` | `30.0` | период цикла |
| `INFERNA_AUTOSCALE_COOLDOWN_SECONDS` | `300.0` | минимальный интервал между действиями по одному деплойменту |
| `INFERNA_AUTOSCALE_TARGET_TTFT_MS` | `1000.0` | верхняя цель (совпадает с порогом кодинга `06` §5) |
| `INFERNA_AUTOSCALE_TARGET_QUEUE` | `4.0` | целевая очередь на реплику (`inferna_instance_active_requests`) |
| `INFERNA_AUTOSCALE_SCALE_DOWN_RATIO` | `0.5` | гистерезис: вниз только при `signal < ratio × target` |

`services/metrics.py` — вне `_ALL_GAUGES` (комментарий почему, как для gateway-метрик `:29-30`):
`inferna_autoscale_actions_total{deployment, action}` (counter, action ∈ up|down|none),
`inferna_autoscale_target_replicas{deployment}` (gauge),
`inferna_autoscale_signal_ratio{deployment, signal}` (gauge: `ttft/target`, `queue/target`).

Проверка: `cd server && uv run pytest` (метрики читаются через `REGISTRY.get_sample_value`);
`INFERNA_AUTOSCALE_ENABLED=true` в dev-стенде не ломает старт (`uv run uvicorn ... --port 8000`).

### Шаг 3 — политика (чистая функция)

`server/inferna_server/services/autoscaling.py`:

```python
@dataclass(slots=True)
class Signal:
    ttft_p95_ms: float | None      # None = нет данных за окно
    queue_per_replica: float | None
    serving_replicas: int

def desired_replicas(*, current: int, window: tuple[int, int], signal: Signal, settings: Settings) -> int
```

Правила: `up`, если `ttft_p95_ms > target_ttft` **или** `queue_per_replica > target_queue`;
`down`, если `ttft_p95_ms < ratio × target_ttft` **и** `queue_per_replica < ratio × target_queue`
(и `serving_replicas > window.min`); иначе `current`. Результат всегда клампится в окно и меняется
не более чем на ±1 за тик. `None`-сигналы трактуются как «нет данных» → `current` (не масштабируем
на слепых метриках).

Источники сигналов (функция `def read_signal(deployment, serving: list[ModelInstance]) -> Signal`):
- `queue_per_replica = sum(REGISTRY.get_sample_value("inferna_instance_active_requests",
  {"instance_id": str(i.id), "model": model_name}) for serving) / max(1, len(serving))`.
- `ttft_p95_ms` — из гистограммы `inferna_ttft_seconds` (M1): цикл хранит предыдущий снапшот сумм
  бакетов по `model` и считает p95 по дельте за прошедший интервал
  (`histogram bucket sum` → линейная интерполяция, утилита `_hist_p95(buckets, counts)` в том же
  модуле, юнит-тестируемая).

Проверка: `server/tests/test_autoscaling.py::test_policy_*` — табличный тест: рост при высоком TTFT;
рост при высокой очереди; no-op в полосе гистерезиса; down только ниже `ratio`; кламп по окну;
±1 за тик; `None` → no-op; `_hist_p95` на синтетических бакетах.

### Шаг 4 — цикл

`services/autoscaling.py`:

```python
async def autoscale_loop() -> None:
    cooldown: dict[uuid.UUID, float] = {}          # deployment_id -> monotonic последнего действия
    while True:
        await asyncio.sleep(settings.autoscale_interval_seconds)
        if not settings.autoscale_enabled: continue
        try:
            async with SessionLocal() as db:
                await _tick(db, cooldown)
        except asyncio.CancelledError: raise
        except Exception: logger.exception("autoscale tick failed")
```

`_tick`: выбрать деплойменты с `max_replicas > min_replicas` (т.е. с заданным окном);
для каждого (отдельная транзакция!): `select(Deployment).where(id==...).with_for_update()` →
`selectinload(instances)` → посчитать `serving` (state == "running"), `read_signal`, `desired_replicas`;
если cooldown не истёк или решение == текущему → `action="none"` (счётчик), иначе
`apply_scale(db, deployment, target, set_range=False)` и запись в `cooldown`. Логировать
`logger.info("autoscale decision", deployment_id, action, inputs=..., target=...)`.

Инварианты порядка блокировок соблюдены: сначала `Deployment FOR UPDATE`, потом внутри
`apply_scale` → `allocate_replicas` лочит воркеров.

`main.py` — пятая задача в lifespan (`:110-115`) + в кортеж отмены (`:130-140`), `app.state.autoscale_task`.

Проверка: `server/tests/test_autoscaling.py::test_tick_scales_up_on_queue` (стаб-сигнал через
подмену `read_signal`), `::test_tick_respects_cooldown` (два тика подряд → одно действие),
`::test_tick_skips_pinned_window` (min == max → ни одного действия),
`::test_tick_disabled_by_setting`, `::test_loop_cancellable`; плюс Postgres-тест в
`test_scheduler_concurrency.py`: параллельный `PUT /range` + автоскейлер-тик не создают
дубликатов портов и не ломают окно.

### Шаг 5 — replay реального трейса

- В `bench/` (корневой пакет из `06` M1, план `plan-06-distributed-inference.md`) добавить
  `bench/replay.py` и режим `--replay FILE`: переигрывает записанный JSONL (`ts, method, path,
  body`, формат пишет `bench` в M1) с сохранением межзапросных интервалов и заданной concurrency
  через gateway.
- `scripts/autoscale-replay.sh` (новый, bash+curl+jq): поднимает деплоймент с окном
  (`PUT /range`), включает автоскейлинг (`INFERNA_AUTOSCALE_ENABLED=true` у сервера),
  запускает `python -m bench --replay <trace> --mode gateway`, параллельно снимает метрики
  (`inferna_autoscale_actions_total`, `inferna_autoscale_target_replicas`) раз в 5 с, пишет JSONL
  в `artifacts/autoscale-replay-<ts>.jsonl`, печатает markdown-таблицу (действия, окно, latency p50/p95).
- Гейт (`02` acceptance): без осцилляций — не более `ceil(duration_minutes / cooldown_minutes) + 2`
  действий за прогон; при масштабировании вверх целевое число реплик достигнуто (нет «половин»
  состояния: у каждой живой реплики `state == "running"` либо `desired_state == "running"`).

### Шаг 6 — Phase 2: масштабирование целыми группами (`06` M8)

После появления `parallel_group_id` (M2.1) `apply_scale` должен считать **реплики = группы**:

- `scaling.py`: единица `live/serving/pending` — группа (`parallel_group_id or instance.id`);
  scale-out создаёт группу целиком (`allocate_parallel_replicas`), scale-in останавливает группу
  целиком (все ранги получают `desired_state="stopped"` + `generation += 1`); счётчик `replicas`
  сравнивается с числом групп.
- Guard в автоскейлере: деплоймент с группами обрабатывается только этой ветвью; до неё
  (`06` M2.2 без M8) автоскейлер такие деплойменты пропускает
  (`if any(i.parallel_group_id for i in instances): action="none"`), чтобы полугрупп не возникало.
- Тесты: `server/tests/test_autoscaling_groups.py::test_scale_up_creates_whole_group`,
  `::test_scale_down_stops_all_ranks`, `::test_no_half_group_after_tick`;
  расширить `test_scheduler_concurrency.py` для групп (Postgres).
- UI (`w3/frontend-parallel` владеет в `06`): отображение рангов/групп — здесь только не ломать
  существующий бейдж; счётчик `ready/current (min–max) running` из шага 7 остаётся.

### Шаг 7 — фронт

- `frontend/src/api/types.ts` — окно уже есть (`Deployment.min_replicas/max_replicas`); добавить
  `useSetDeploymentRange()` в `hooks.ts` (`PUT /deployments/{id}/range`, инвалидация
  `["deployments"], ["instances"]`).
- `frontend/src/pages/InstancesPage.tsx`: заголовок группы показывает
  `ready/current (min–max) running`; добавить инлайн-контролы `Min`/`Max` (по образцу существующих
  кнопок Scale up/down, `:98-126`) с подсказкой «Range enables autoscaling; Scale up/down sets an
  exact count»; при `min == max` бейдж «autoscaling off», при `max > min` — «autoscaling on».
- Юнит-тест `frontend/src/__tests__/InstancesRange.test.tsx` (stub fetch): отправка `PUT /range`;
  рендер бейджа окна. e2e: расширить `frontend/e2e/scale-group.spec.ts` — задать окно 1–3, увидеть
  бейдж `1–3`, затем Scale down → окно сохранилось (после него `min=max`, поэтому проверять
  последовательность: сначала range-контролы).

## Verification (сводно)

1. `cd server && uv run ruff check . && uv run pyright && uv run pytest`.
2. Postgres: `INFERNA_DATABASE_URL=postgresql+asyncpg://... uv run pytest tests/test_scheduler_concurrency.py -v`.
3. `cd server && uv run python ../scripts/export-openapi.py --check`.
4. `cd frontend && npm run lint && npx tsc --noEmit && npx vitest run`.
5. Replay: `bash scripts/autoscale-replay.sh --trace <записанный трейс>` → таблица, действий не
   больше порога, «половин» групп нет, JSONL в `artifacts/`.
6. Приёмка `02` Release B: политика проходит replay без осцилляций; scale-out/scale-in не снимают
   живые реплики; окно сохраняется при автоскейлинге.

## Assumptions & contingencies

- **Cooldown в памяти** — рестарт сервера даёт одну дополнительную оценку; если это недопустимо,
  перенести cooldown в `Deployment.last_scaled_at` (правка семантики Release A + её тестов).
- **Политика стартует с TTFT+очереди**; если M1-гистограмма окажется недостаточно стабильной на
  mock-стенде, тесты используют стаб `read_signal` (политика юнит-тестируется независимо от метрик).
- **`INFERNA_AUTOSCALE_ENABLED=false` по умолчанию** — ни один существующий стенд и e2e не
  меняют поведение; e2e, проверяющий окно, включает автоскейлинг только в своём прогоне.
- **Волна E**: этот план может исполняться параллельно `w3/parity-evidence` и
  `w2/cache-aware-routing` (разные файлы); `services/scaling.py` в волне E принадлежит только
  этому плану.
- Если `06` M2.2 не влит (волна D не закрыта), Phase 1 (шаги 1–5, 7) исполним и без групп —
  guard-ветка «пропускать группные деплойменты» тогда просто не срабатывает; Phase 2 (шаг 6)
  обязателен для гейта M8.

## Evidence Artifacts

`evidence-p02b-autoscaling.md` рядом с планом (в `docs/roadmap/plans/`, коммитится): шаг → команда → сокращённый вывод → результат →
дата. Артефакты replay — `artifacts/autoscale-replay-*.jsonl` (git-ignored).