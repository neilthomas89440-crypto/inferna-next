> **SUPERSEDED — не исполнять.** Заменён планом `plan-06-distributed-inference.md`
> (M0–M9, 16 веток) + `roadmap-program-03-06.md`. Причина отказа: (1) нет `w0/multinode-contract` —
> параллельные поля не доходят до воркера; (2) rendezvous хранился в `error_detail`, который
> стирается `services/workers_svc.py:219` на первом Sync → ломался инвариант «трафик только на
> rank 0»; (3) `node_ids/master_host/master_port` задавал пользователь вместо выборов платформы и
> порт-матрицы §9.1; (4) bench в `server/bench/` вместо корневого `bench/` (ownership §7);
> (5) mock поднимал OpenAI-сервер с конфликтующим портом; (6) «кодинг-валидация» заявлялась без M6.
> Файл сохранён как история решения.

# Multi-node MVP: одна модель на двух машинах + кодинг-валидация

Слаг: `multi-node-mvp`

## Context

Запрос: реализовать план из `docs/roadmap/06-distributed-inference.md` — «несколько машин → одна
модель размазана → кодинг через `/v1`». Полный план (M0–M9, 16 веток, 5 волн) рассчитан на
параллельную работу отдельных агентов и выходит за рамки одной сессии. Этот файл — первый
самодостаточный срез, закрывающий минимальный проверяемый контур (подмножество M0+M2+M6
роадмапа):

1. `INFERNA_MOCK_ENGINE=true` воркер умеет multi-node (parallel group из 2 рангов).
2. REST-деплой создаёт одну параллельную группу на двух воркерах; gateway ходит только на rank 0.
3. Реальный runtime (docker) строит multi-node vLLM/SGLang команды.
4. Репорт `scripts/parity.sh --mode smoke` зелёный на локальном mock-стенде.

Режим параллелизма для MVP: `pp` (pipeline parallel, world_size=2) — выбран потому, что по
физике (роадмап §2) `pp` пересылает только активации (~16 KiB/микробатч, не зависит от длины
контекста) и остаётся применим на нестязанных сетях, тогда как `tp` требует allreduce на каждом
слое и жив только на DC-фабрике. Движок для валидации на реальном железе — vLLM; SGLang —
команды тем же кодом, живая проверка вне скоупа этого среза. Бенчмарк-харнесс (M1 роадмапа)
включён в объём: без него кодинг-валидация не измеряется.

Контекст roadmap: описание хранится в `docs/roadmap/06-distributed-inference.md`; этот файл не
заменяет его, а уточняет первый срез. Разделы roadmap, на которые ссылается план: §0 (конечный
сценарий), §3 (инварианты), §5 (пороги кодинга: TTFT p95 ≤ 1.0 s, ITL ≤ 50 ms, cache-hit ≥ 80 %),
§6 (M2 — контракты и runtime), §7 (владение файлами). Осторожно: roadmap-файл был создан
planning-агентом в основном контексте; в контексте имплементатора он может отсутствовать, поэтому
все load-bearing решения продублированы ниже literal.

Рабочая директория: `C:/Users/admin/Desktop/projects/inferna-next`.

## Prerequisites

- Ветка `phase2-replicas-autoscaling` (текущая). Слияние в `main` — отдельное решение; здесь
  НЕ выполняется.
- `cd server && uv sync --dev` и `cd worker && uv sync --dev` зелёные; `uv run pytest` в обеих
  директориях зелёный на старте. Если нет — остановиться и записать блокер в раздел
  «Evidence Artifacts», дальше не идти.
- Валидация на реальном железе (шаги 3.5, 4.6): НЕ запускается в этой сессии. На двух
  GPU-хостах с публичными адресами — после approve; в отдельной сессии запускается
  `scripts/parity.sh --mode multi`.
- Проверка флагов multi-node в pinned-образе (первое действие шага 3.1) обязательна.

## Общие правила выполнения

- Каждый шаг завершается полным набором проверок шага; проверки НЕ переносятся в конец.
- Все находки и результаты наблюдений записываются в файл «Evidence Artifacts» (создать его
  при первом наблюдении). Отдельные артефакты не создаются.
- Новые python-файлы: только пакеты `inferna_server`/`inferna_worker` и `server/bench/`
  (см. шаг 2). Python 3.12, uv. Линт: `uv run ruff check .` и `uv run pyright` в `server/`
  и `worker/`; line-length 100; structlog для логов.
- Порядок шагов менять нельзя: шаг 3 (воркер) и шаг 4 (сервер) можно вести параллельно только
  при наличии двух исполнителей; шаги 1 → 2 → 5 строго последовательны.
- Правки прото или alembic в этом срезе НЕ выполняются (обходный путь описан в шаге 3.1).

## Approach

### Шаг 1 — репорт-скрипт и скелет бенчмарка (parity.sh)

Файлы: `scripts/parity.sh` (новый), `scripts/bench.sh` (новый).

`scripts/parity.sh` — единственный вход для запуска сценария «одна модель на двух воркерах»:

- CLI: `bash scripts/parity.sh --mode {smoke|single|multi|full} [--model-id ID]
  [--engine {vllm|sglang}] [--profile {latency|throughput}]` и env-параметры:
  `INFERNA_API` (default `http://localhost:8000`), `INFERNA_ADMIN_USER`/`INFERNA_ADMIN_PASSWORD`
  (default `admin`/`inferna`), `INFERNA_GATEWAY_KEY` (API-ключ; если пуст — создать через
  `POST /api/v1/keys`), `WORKER_A_NAME`/`WORKER_B_NAME` (default `mn-a`/`mn-b`).
- Поведение по режимам:
  - `smoke` (default, локальный mock-стенд): deploy multi-node (через API из шага 4; до
    готовности шага 4 печатает `DEPLOY: NOT AVAILABLE — run after step 4` и exit 2),
    ожидание `running` всех рангов, `GET /v1/models`, один streaming-запрос `/v1/chat/completions`
    через gateway с замером TTFT/e2e, bench (после шага 2; до него печатает
    `BENCH: NOT AVAILABLE — run after step 2` и exit 2 — оба маркера удаляются в шагах 2 и 4).
  - `single`: deploy обычной single-node реплики, bench, пишет baseline.
  - `multi`: то же + multi-node deploy. Требует шаги 3+4 и живые воркеры.
  - `full`: `single` + `multi` + сравнение коэффициентов (после шагов 2–5).
- Вывод: строки JSONL в `parity-$(date +%Y%m%d-%H%M%S).jsonl` и markdown-таблица в stdout.
  Каждая строка JSONL: `{"ts": iso8601, "step": "deploy|ready|chat|bench|cleanup", ...}`.
  Строки с результатами — только с реальными измерениями (`deploy_ms`, `ready_ms` per rank,
  `ttft_ms`, `e2e_ms`, `status`). Статус-заглушки вида `SKIPPED` допускаются только для
  зависящих от будущих шагов веток режимов и удаляются при реализации этих шагов.
- Exit-код: 0 при успехе (все ранги `running`, chat 200, bench отработал), 1 при провале,
  2 при недоступности зависимостей (шаги 2/4 ещё не сделаны).
- Зависимости: `curl`, `jq`, `python3`. Дату узнавать через `date`, `instance_id` — из JSON
  ответа. Ничего из `bench/` (шаг 2) на этом шаге не вызывается — только `curl`+`jq`
  (bench-часть после шага 2 — через `bash scripts/bench.sh ...`, см. ниже).

Проверка шага 1 (после выполнения — НЕ раньше):
`bash scripts/parity.sh --mode smoke` печатает `PARITY SMOKE: NOT AVAILABLE — run after steps 2 and 4`
и exit 2 (допустимо до шагов 2/4; это не заглушка-обман: ровно одно сообщение с причиной
«deploys multi-node and measures chat; blocked by API/bench from steps 2 and 4»).

Проверка на реальном железе недоступна в этой сессии (нет двух GPU-хостов); реализация
проверяется на mock-стенде (шаг 5).

### Шаг 2 — bench-харнесс

Новый пакет `server/bench/` (в составе server uv-проекта; в pyproject dependencies добавить
ничего не нужно — httpx уже есть). Модули: `__init__.py`, `__main__.py`, `client.py`,
`scenarios.py`, `metrics.py`, `report.py`. Тесты: `server/tests/test_bench.py` (новый).
Документация сценария — в файл «Evidence Artifacts» (не в `docs/`).

CLI:
```
uv run python -m bench --api http://localhost:8000 --api-key <inf-key> --model-id <uuid> \
  --scenario {single-burst|agentic-loop|long-context} --conc 1 --repeats 3 --duration 30 \
  --mode {gateway|direct} [--direct-base http://host:port] --out bench-out/<scenario>-<ts>.jsonl
```

- `client.py`: `async def chat(client: httpx.AsyncClient, base_url: str, key: str,
  model_name: str, messages: list[dict], stream: bool = True, max_tokens: int = 128) ->
  RequestResult`. SSE-парсинг по строкам `data: `; TTFT = время первого content-чанка;
  ITL = интервалы между content-чанками; usage из последнего чанка. `RequestResult` —
  dataclass: `status:int, ttft_ms:float|None, e2e_ms:float, itl_ms:list[float],
  prompt_tokens:int|None, completion_tokens:int|None, stream_error:str|None`.
- `scenarios.py` — три сценария, все с `temperature: 0`, фиксированные промпты (детерминизм):
  - `single-burst`: N = conc×duration запросов «Напиши функцию быстрой сортировки на Python
    с docstring» + max_tokens 128, concurrency = `conc`.
  - `agentic-loop`: 8 итераций; промпт = PREFIX (≈4k токенов: сгенерированный фиксированный
    текст с seed 42) + «текущая итерация: i»; имитирует переотправку контекста агентом,
    выявляет работу префикс-кэша.
  - `long-context`: промпт 32k токенов (генерация с seed 7), max_tokens 64.
- `metrics.py`: `def pct(values: list[float], p: float) -> float` (linear interpolation),
  `def tps(completion_tokens: int, e2e_ms: float) -> float`.
- `report.py`: markdown-таблица (p50/p95/p99 TTFT и e2e, mean tokens/s, число ошибок) —
  единственный формат вывода результатов.
- Режим `direct`: тот же `client.chat` с `base_url` из `--direct-base`, минуя gateway —
  измеряет overhead прокси.
- Конфликт None-значений (нет чанков, нет usage): записывать `null` в JSONL, ошибки —
  отдельной строкой `{"stream_error": ...}`; НЕ эмулировать значения.
- Пустой ответ / 404 / 401: писать строку с `status` и НЕ останавливать весь прогон;
  останавливать только при >50 % ошибок.

Тесты (`server/tests/test_bench.py`, docstring «Bench harness: SSE metrics and scenarios»):
`test_pct_interpolates`, `test_tps`, `test_client_parses_mock_sse` (httpx MockTransport c
фиксированным SSE-стримом), `test_scenario_agentic_prefix_grows`.

Проверка шага 2 (на mock-стеке; как поднять — шаг 5): `bash scripts/bench.sh --scenario
single-burst --conc 1 --repeats 3` печатает таблицу, три повтора дают max-разброс p50 ≤ 5 %.

`scripts/bench.sh` — тонкая обёртка: `cd server && uv run python -m bench "$@"` c
проверкой env.

### Шаг 3 — multi-node runtime в воркере

Вход: `worker/inferna_worker/engines/base.py` (сегодня: `build_command(config)` с
`tensor_parallel = max(1, len(config.gpu_indexes))`, база vLLM
`["vllm","serve","{model}","--port","8000","--gpu-memory-utilization","0.9","--tensor-parallel-size","{tp}"]`).

3.1. Командный билдер. Новая dataclass в `base.py`:
```python
@dataclass(slots=True)
class MultiNodeSpec:
    mode: str          # "pp" | "tp"
    world_size: int    # общее число хостов
    node_rank: int     # 0 = голова
    master_host: str   # адрес rank0, доступный остальным рангам
    master_port: int
```
Сигнатура: `def build_command(config: EngineConfig, spec: MultiNodeSpec | None = None,
serve_port: int = 8000) -> list[str]`. Поведение при `spec=None` — байт-в-байт текущее
(существующие тесты `worker/tests/test_engine_flags.py` фиксируют; они не меняются).

Точные argv (флаги профиля vLLM/SGLang дописываются как сегодня, ПОСЛЕ базы):
- vLLM `mode="pp"`, world=2, rank 0, serve_port=8011:
  `["vllm","serve",<model>,"--port","8011","--gpu-memory-utilization","0.9",
  "--tensor-parallel-size",<len(gpu_indexes)>,"--pipeline-parallel-size","2",
  "--nnodes","2","--node-rank","0","--master-addr",<master_host>,"--master-port",<master_port>]`
  затем флаги профиля.
- vLLM rank 1: то же, `--node-rank 1`, БЕЗ `--port` (API-сервер только на голове).
- vLLM `mode="tp"`: вместо `--pipeline-parallel-size` —
  `--tensor-parallel-size = str(len(gpu_indexes) * world_size)`, `--distributed-executor-backend
  mp` (только mode tp), те же `--nnodes/--node-rank/--master-addr/--master-port`.
- SGLang (оба режима): base SGLANG_BASE + `--nnodes <world> --node-rank <rank>
  --dist-init-addr <master_host>:<master_port>`; для `mode="pp"` добавить
  `--pipeline-parallel-size <world>`; для `mode="tp"` — `--tp <len(gpu_indexes)*world>`.
  `--dist-init-addr` — rank 0 и все ранги.
- env для контейнера (возвращает новый `def multi_node_env(spec: MultiNodeSpec, advertise_host:
  str, netif: str) -> dict[str, str]` в `base.py`): `{"NCCL_SOCKET_IFNAME": netif,
  "VLLM_HOST_IP": advertise_host}`.

До написания кода — обязательно: `docker run --rm vllm/vllm-openai:v0.8.5 vllm serve --help
2>&1 | grep -E "nnodes|node-rank|master-addr|master-port|distributed-executor-backend"` —
убедиться, что флаги есть в pinned-образе (сверка с docs.vllm.ai: `--nnodes/--node-rank/
--master-addr/--master-port` и `--distributed-executor-backend`, где `mp` требует `nnodes`).
Если образ недоступен/флагов нет — записать блокер в «Evidence Artifacts», остальные части
шага 3 (mock-режим, адаптер рангов) всё равно выполнить, а живую multi-node валидацию
(шаг 3.5) пометить `BLOCKED: <причина>`.

3.2. Адаптер в `engines/manager.py` (сегодня `InstanceManager` c mock/real ветками,
docker-py, `_start_sync` c `containers.create(..., ports={"8000/tcp": config.port}, ...)`).
Правила перевода полей proto `EngineConfig` в `MultiNodeSpec`:
- режим `single` (поля `world_size=1` или отсутствие) → `spec=None`, поведение как сегодня;
- rank 0 → `node_rank=0` (голова);
- `mode` берётся из поля proto `mode` (значения `single|pp|tp`); `master_port` — из поля proto
  `master_port`; `master_host` — из поля proto `master_host`.
- `master_port=0` или `master_host=""` в конфиге при world>1 → ошибка: воркер помечает
  instance `state="error"`, `detail="multinode requires master_host and master_port"`.

3.3. Real-docker ветка (изменения в `_start_sync`):
- сеть: для multi-node контейнеров `network_mode="host"` (docker-py: `containers.create(...,
  network_mode="host")`, `ports` НЕ передавать — docker запрещает оба). Причина одна строка в
  комментарии кода: NCCL устанавливает входящие соединения между всеми рангами, NAT bridge
  их ломает; host-сеть и `VLLM_HOST_IP` решают это без публикации диапазона портов.
- rank 0: `--port <config.port>` (см. 3.1); port-пул `config.port` уже выделяется сервером.
- rank > 0: контейнер создаётся БЕЗ опубликованных портов, ports = None.
- `environment` в `containers.create` = `{**multi_node_env(spec, advertise_host, netif),
  **({"HF_HUB_TOKEN": ...} как сегодня)}`.
- readiness: rank 0 — текущий health-probe `/v1/models` (порт `config.port`); rank > 0 —
  новый статус `state="running"` выставляется, когда docker-статус контейнера `"running"`
  непрерывно ≥ `INFERNA_MN_RANK_READY_SECONDS` (default `20.0`); до этого `state="starting"`.
  Реализация: расширить существующий `_health_probe` веткой `if spec and spec.node_rank > 0`
  (poll docker-статуса каждые 10 c вместо HTTP).
- stop/delete: без изменений — генерация и `_stop_sync/_delete_sync` работают по-прежнему.

3.4. Mock-ветка (`INFERNA_MOCK_ENGINE=true`):
- config воркера (`worker/inferna_worker/config.py`): `mock_multinode: bool = False`
  (`INFERNA_MOCK_MULTINODE`), `mock_openai_port: int = 8010` (`INFERNA_MOCK_OPENAI_PORT`),
  `advertise_host: str = ""` (`INFERNA_WORKER_ADVERTISE_HOST`, default — `platform.node()`),
  `netif: str = "eth0"` (`INFERNA_WORKER_NETIF`), `mn_rank_ready_seconds: float = 20.0`
  (`INFERNA_MN_RANK_READY_SECONDS`).
- воркер с `INFERNA_MOCK_MULTINODE=true` стартует настоящий OpenAI-сервер (порт
  `settings.mock_openai_port`), который обслуживает `/v1/chat/completions` (SSE, модель
  возвращается как эхо) и `/v1/models`; readiness: HTTP 200 на `/v1/models`.
- multi-node в mock: ранги ведут себя по тем же правилам адаптера (rank 0 — head, ранг > 0 —
  помечается running после `MOCK_START_DELAY_SECONDS` без HTTP). Ранги НЕ соединяются по сети.
- server (`api/gateway.py`) для mock-стенда: `--direct-base` уже задаёт точку входа; gateway
  НЕ запускает внутренний OpenAI-сервер (модель подключается, как в проде, через
  `direct_base`).

Тесты (новые): `worker/tests/test_engine_flags.py::test_vllm_multinode_pp_rank0_and_rank1`,
`::test_sglang_multinode_uses_dist_init_addr`, `worker/tests/test_manager_multinode.py::
test_mock_multinode_lifecycle`, `::test_mock_rank0_serves_openai_on_instance_port`
(httpx-запрос к поднятому серверу), `worker/tests/test_manager_multinode_real.py::
test_real_multinode_container_host_network` (fake docker: порты None + network_mode host —
pattern fake-docker из существующих тестов, `worker/tests/test_manager_mock.py`).

3.5. Живая multi-node валидация (два GPU-хоста) — не запускается в этой сессии. Скрипт
`scripts/parity.sh --mode multi` уже реализован шагом 1; запуск на железе — отдельная сессия
(записать в «Evidence Artifacts» как deferred с командой запуска).

### Шаг 4 — deploy pipeline и gateway

4.1. Адаптер ручного размещения: `server/inferna_server/api/instances.py` (деплой сегодня:
`allocate_*` → создание `Deployment` + `ModelInstance`-строк → commit). Добавить в
`DeployRequest` (в `schemas.py`) необязательное поле `parallelism: ParallelismRequest | None`
```python
class ParallelismRequest(BaseModel):
    mode: Literal["pp", "tp"]
    node_ids: list[uuid.UUID]           # ровно 2: [rank0, rank1]
    tp_size: int = Field(default=1, ge=1, le=8)
    master_host: str = Field(min_length=1)
    master_port: int = Field(ge=1024, le=65535)
```
`node_ids` — id существующих worker'ов кластера (минимум 2). Валидация: оба worker'а
`state == "connected"`; иначе 400 `"multinode requires two connected workers"`.
`gpu_selection` с `parallelism` несовместимы: 400 `"parallelism is not supported with manual gpu selection"`.

4.2. Scheduler: `server/inferna_server/services/scheduler.py`. Существующая
`allocate_replicas(db, cluster_id, vram_required_mb, engine, count)` НЕ меняется. Добавить
функцию `async def enumerate_multinode_placements(db, cluster_id, engine, node_ids: list
[uuid.UUID]) -> list[tuple[Worker, int, int]]`: для каждого worker из `node_ids` выбирает
лучший GPU по существующему best-fit (`_best`/`_gpu_usage` — переиспользовать как есть) и
свободный порт (`_alloc_port`), возвращает `(worker, gpu_index, port)` в порядке рангов
(ранг = позиция в node_ids). Блокировки: переиспользовать существующий паттерн — тот же
`SELECT ... FOR UPDATE` на строках Worker (см. `_connected_workers` / `allocate_manual`).
Отдельная блокировка НЕ создаётся.

4.3. Создание группы: в том же обработчике POST создать одну `Deployment`-строку (как
сегодня для реплик) и N `ModelInstance`-строк: существующие колонки заполняются как сегодня,
плюс в `error_detail` — serialization-заметка, а точные значения видны через новый
API-ответ (см. 4.4). Никаких новых полей БД нет (обходной путь шага 3.1; риск коллизий
`(parallel_group_id, node_rank)` принят: контролируется одним обработчиком).

4.4. Ручной rendezvous и gateway. Команды для воркера (в `services/workers_svc.py`,
функция, которая сегодня строит `InstanceCommand` с `EngineConfig`) получают multi-node
поля, если инстанс из параллельной группы: `mode`, `world_size=len(node_ids)`,
`node_rank` (позиция), `master_host`, `master_port` — источники: serialization-заметка в
`error_detail` (формат: `mn:mode=pp;world=2;rank=0;master=hostA:8111` — парсить/писать строго
этой функцией, хелперы `_mn_pack(inst)/_mn_unpack(str)` в том же файле).
Gateway (`api/gateway.py`, выбор инстанса `min(rows, key=lambda i: (_active_by_instance.
get(i.id,0), _last_assigned.get(i.id,0)))`): добавить фильтр `rows = [i for i in rows if not
(i.error_detail or "").startswith("mn:") or _mn_rank(i) == 0]` (строго: строки с префиксом
`mn:` и рангом ≠ 0 исключаются; обычные инстансы не затрагиваются). `InstanceOut` в
`schemas.py` получает поле `multinode: dict | None` (значение `_mn_unpack`, только для
групп; None для обычных). После изменения схем: `cd server && uv run python
../scripts/export-openapi.py` (и закоммитить `docs/openapi.yaml`).

4.5. Тесты (все через существующие фикстуры `server/tests/conftest.py`:
`add_connected_worker`, `deployment_factory`, `login`, `auth_headers`):
- `server/tests/test_instances_multinode.py::test_deploy_multinode_creates_group_of_two` —
  POST с `parallelism` создаёт 2 инстанса, у обоих `_mn_unpack(error_detail)` возвращает
  ожидаемое; обе строки scheduled.
- `::test_sync_commands_carry_rendezvous_and_ranks` — Sync первому воркеру отдаёт `start`
  c `mode/world/rank/master` (через `_mn_pack/_mn_unpack`-контракт).
- `server/tests/test_gateway_multinode.py::test_gateway_selects_only_rank0` — из двух живых
  инстансов группы выбирается только rank 0 (обе строки live; прямой запрос через gateway
  endpoint с мок-транспортом).

4.6. Живая валидация на железе — deferred (см. 3.5).

### Шаг 5 — интеграция: паритет-репорт, e2e, обновление parity.sh

5.1. Обновить `scripts/parity.sh --mode smoke` до полного сценария: deploy multi-node (mock),
  ожидание running обоих рангов, запуск `bash scripts/bench.sh --mode direct` и
  `--mode gateway` (обе таблицы в stdout), сравнение gateway/direct overhead. Удалить оба
  `NOT AVAILABLE`-маркера шага 1 и exit-код 2 для них.

5.2. E2E-тест: `server/tests/test_deploy_multinode_e2e.py::test_full_multinode_flow` —
  два подключённых mock-воркера (fixture `add_connected_worker` ×2), POST с `parallelism`,
  симуляция Sync-цикла (вызвать функцию построения команд из `workers_svc` как в
  существующих `test_workers_sync.py`), установка состояния через прямое обновление строк,
  запрос `/v1/chat/completions` через `gateway` с `httpx.AsyncClient` (transport на
  `mock_openai_port` mock-сервера из шага 3.4), assert 200 + streaming-чанки.

5.3. Обновить «Evidence Artifacts» в конце: full checklist с результатами каждого шага
  (проверка шага, артефакт, статус). Единственный документ, обновляемый этим планом.

5.4. Commit-разбивка: один commit на шаг (1–5); сообщения `feat(bench): ...`,
  `feat(worker): multinode runtime`, `feat(server): parallel deploy + gateway rank0`,
  `feat(e2e): parity smoke green`. В основной ветке (по умолчанию: текущая ветка
  `phase2-replicas-autoscaling`, без main).

## Critical files & anchors

- `worker/inferna_worker/engines/base.py` — `build_command`/`MultiNodeSpec`/`multi_node_env`,
  точные argv (шаг 3.1).
- `worker/inferna_worker/engines/manager.py` — `_start_sync`/`_health_probe`: host-сеть,
  env, readiness рангов (шаги 3.2–3.4).
- `server/inferna_server/api/instances.py` — `DeployRequest.parallelism` → создание группы
  (шаг 4.1, 4.3).
- `server/inferna_server/services/workers_svc.py` — `_mn_pack/_mn_unpack` + команды с
  rendezvous (шаг 4.4).
- `server/inferna_server/api/gateway.py` — фильтр rank 0 в выборе инстанса (шаг 4.4).

## Evidence Artifacts

Создать файл `multi-node-mvp-evidence.md` в корне рабочей директории при первом наблюдении.
Внутри: по каждому шагу — фактическая команда проверки, вывод (сокращённый), результат,
дата. Дополнительные файлы наблюдений не создаются; артефакты прогона (`parity-*.jsonl`,
`bench-out/*.jsonl`) остаются в рабочей директории (git-ignore не настраивать — артефакты
рабочей сессии, не коммитить). Все файлы, упомянутые в плане, — рабочая директория
`C:/Users/admin/Desktop/projects/inferna-next`.

## Verification

Финальная проверка (все команды из `C:/Users/admin/Desktop/projects/inferna-next`):

1. `cd server && uv run ruff check . && uv run pyright && uv run pytest` — зелёные.
2. `cd worker && uv run ruff check . && uv run pyright && uv run pytest` — зелёные.
3. `cd server && uv run python ../scripts/export-openapi.py --check` — зелёный.
4. Mock-стенд (3 терминала, каждый живёт до конца проверки):
   - `cd server && uv run uvicorn inferna_server.main:app --port 8000` (или существующий
     dev-скрипт, см. README репо — запустить как в существующем e2e: `docker compose -f
     docker-compose.yml -f docker-compose.mock.yml up -d --build postgres server`);
   - воркер A: `cd worker && INFERNA_SERVER_URL=localhost:9091
     INFERNA_WORKER_NAME=mn-a INFERNA_MOCK_ENGINE=true INFERNA_MOCK_MULTINODE=true
     INFERNA_MOCK_OPENAI_PORT=8010 uv run python -m inferna_worker`;
   - воркер B: то же с `INFERNA_WORKER_NAME=mn-b`, `INFERNA_MOCK_OPENAI_PORT=8012`.
5. `bash scripts/parity.sh --mode smoke --engine vllm` — ожидание: оба ранга `running`,
   chat через `/v1` вернул 200 со стримом, обе bench-таблицы напечатаны, exit 0.
6. `bash scripts/bench.sh --scenario agentic-loop --conc 1 --repeats 3` — таблица, p50
   повторов в пределах 5 %.
7. `cd frontend && npx tsc --noEmit && npx vitest run` — зелёные (frontend не менялся;
   проверка на отсутствие случайных поломок).

Ни один из пунктов 1–6 не выполняется в этой сессии — сессия planning-only; пункты 1–7
выполняются при исполнении плана (кроме живой валидации на GPU — см. 3.5/4.6).

## Assumptions & contingencies

- **Многопользовательская DB-схема не создаётся.** Группа помечается только через
  `error_detail`-serialization (`mn:...`); schema-level обеспечения уникальности нет.
  Риск: деградация диагностики при ошибках; принято для MVP.
- **Параллелизм и БД-состояния групп не поддерживаются** (это объём M2 полного плана,
  отдельная волна работ; см. roadmap). Этот срез — «smoke-уровень» контура, не финальная
  архитектура.
- **Flags в pinned-образе.** vLLM `v0.8.5`: `--nnodes/--node-rank/--master-addr/--master-port/
  --distributed-executor-backend` (mp+nnodes) — сверено с docs.vllm.ai на момент планирования;
  `unverified — confirm first` для pinned-тега (команда в 3.1). Если флага нет: блокер
  записать, mock-часть и адаптер всё равно доделать, живую валидацию пометить BLOCKED.
- **SGLang**: `--nnodes/--node-rank/--dist-init-addr` подтверждены issue sgl-project/sglang
  #22749 и docs.sglang.io; живая проверка вне скоупа (см. 3.5).
- **Гонки `error_detail`-нотации**: `_mn_pack` перезаписывается только при создании инстанса
  и при lifecycle-событиях (не в цикле Sync); если реальная имплементация потребует
  перезаписи — поле должно дополняться, а не заменяться (контракт `_mn_pack/_mn_unpack`
  сохраняется).
- **`containers.create` с `network_mode="host"` и `ports=None`** — docker-py поднимет
  ValueError при одновременной передаче; передача только host-режима — норма.
- **`worktree` и генерация proto**: правки прото и alembic в этом срезе НЕ выполняются
  (нет изменений контракта — mock-режим теста и адаптер закрывают потребность). Если
  при исполнении выяснится, что контракта недостаточно (например, потребуется
  `world_size` в статусе инстанса), остановиться и записать блокер — НЕ расширять
  прото самовольно.
- **Живая валидация на реальном железе** — обязательный итог этапа, но требует два
  GPU-хоста; в этой сессии фиксируется как deferred с готовой командой
  (`scripts/parity.sh --mode multi`). Если железа нет — статус в Evidence Artifacts:
  `BLOCKED: no two-GPU-host environment available`.
- **Шаги 3 и 4** (worker/server) могут выполняться параллельно при двух исполнителях;
  при одном — порядок 3 → 4.
- **Режим деплоя для multi-node в этом срезе** — автоматический (gpu_selection=auto):
  `node_ids` фиксированы, а GPU/порты выбирает планировщик (шаг 4.2). Ручной выбор GPU
  для multi-node не поддерживается.
- **Cleanup**: любые временные скрипты/файлы, созданные при исполнении (кроме
  перечисленных в плане), удаляются в конце шага 5.3.
