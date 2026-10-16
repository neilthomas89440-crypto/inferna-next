# План: Distributed inference, M0–M9 (`docs/roadmap/06-distributed-inference.md`)

Слаг: `plan-06-distributed`. Ветка-база: `w0/multinode-contract` и далее по карте §7 роадмапа.
Роль: **исполняемый план всего направления 7** (заменяет `multi-node-mvp-plan.md`, см. §0.3).

Источник требований — `docs/roadmap/06-distributed-inference.md` (далее «роадмап»): §2 физика,
§3 инварианты, §5 пороги, §6 вехи, §7 карта веток, §9 порт-матрица. Этот файл не переписывает
роадмап, а фиксирует *как* и *в каком порядке* он исполняется, с точными якорями по текущему коду.

## 0. База и ограничения

### 0.1 Что есть сегодня (проверено чтением)

- `proto/cluster.proto` — только `Register`/`Sync`; `EngineConfig` = 7 полей (`:51-59`);
  `InstanceStatus` = 5 полей (`:43-49`). Ни `Parallelism`, ни `Rendezvous`, ни RPC логов.
- `worker/inferna_worker/engines/base.py:51-65` — `build_command(config)`, `tensor_parallel =
  max(1, len(config.gpu_indexes))`, `VLLM_BASE` с `--port 8000` (`:10-23`); образы —
  `vllm/vllm-openai:v0.8.5`, `lmsysorg/sglang:v0.4.6.post1` (`config.py:28-29`).
- `engines/manager.py` — `_start_sync` создаёт контейнер с `ports={"8000/tcp": config.port}`
  (`:312`) и bridge-сетью; `_health_probe` бьёт `http://127.0.0.1:{port}/v1/models` (`:394`);
  `statuses()` (`:460-478`); mock-ветка без HTTP (`_start_mock:218-238`).
- `server/inferna_server/services/scheduler.py` — `PORT_RESERVED:16-19`, `_alloc_port:49-55`,
  `_connected_workers` (`FOR UPDATE`)`:58-65`, `allocate_auto:68-90`, `allocate_manual:93-136`,
  `allocate_replicas` (anti-affinity Pass 1a/1b/2)`:139-199`.
- `services/workers_svc.py:244-256` — сборка `EngineConfig` для `start`; `:219` —
  `inst.error_detail = status.detail or None` на **каждом** Sync.
- `api/gateway.py:369-400` — выбор инстанса: `ModelInstance.state.in_(LIVE_STATES)`,
  `port.isnot(None)`, `min(rows, key=lambda i: (_active_by_instance..., _last_assigned...))`.
- `models.py:165-204` `Deployment` (нет `nodes_per_replica`/`parallel_mode`), `:201-261`
  `ModelInstance` (нет параллельной группы), `:253` `error_detail Text`.
- Докер/CI/скрипты: один воркер в `docker-compose.yml:45-59` без портов и с единым
  `INFERNA_WORKER_ADDRESS=host.docker.internal`; `.github/workflows/ci.yml` джобы `backend`
  (matrix server/worker), `migrations`, `proto-check`, `openapi-check`, `frontend`, `e2e`;
  скриптов `bench.sh`/`parity.sh`/`netem.sh`/`wan-envelope.sh`/`coding-validation.sh`/
  `soak-multinode.sh` нет; `docs/multinode-bench.md`, `multinode-parity.md`, `coding-validation.md`,
  `network-matrix.md`, `CHANGELOG.md` нет; `bench/` нет.

### 0.2 Жёсткие правила этого плана

- **Контракт замораживается в M2.1** (роадмап §7): proto и схему БД после `w0` меняет только
  `w4/diagnostics` (RPC логов) и `w4/usage-metering` (своя ревизия) — и никогда одновременно.
- **Один писатель на файл**: `api/gateway.py` (порядок: `w1/gateway-telemetry` →
  ранг-фильтр из `w2/server-parallel-groups` → `w2/cache-aware-routing`);
  `services/metrics.py` (в том же порядке); `models.py`/`alembic/versions` (`w0` → `w4/usage-metering`);
  `services/scaling.py` (`w2/server-parallel-groups` → `w4/autoscaling`).
- После правки proto — `bash scripts/gen-proto.sh` + коммит стабов (CI `proto-check`);
  после правки REST-схем — `export-openapi.py` + коммит `docs/openapi.yaml`.
- Новые env — `INFERNA_*`, в `.env.example` и `config.py` (AGENTS.md).
- Пороги — только из роадмапа §5; режим вне порога публикуется как «непригоден при RTT > X ms»,
  а не как провал.

### 0.3 Дельта к `multi-node-mvp-plan.md` (не исполнять его)

Заменён целиком, потому что: (1) в нём не было `w0`, без которого параллельные поля физически не
доходят до воркера; (2) rendezvous хранился в `error_detail`, который стирается
`workers_svc.py:219` на первом же Sync (воркер при успехе отдаёт `detail=""`,
`manager.py:423`) → инвариант «трафик только на rank 0» ломался; (3) `node_ids/master_host/
master_port` задавал пользователь вместо выборов платформы и порт-матрицы §9.1; (4) bench жил в
`server/bench/` вместо корневого `bench/`; (5) mock поднимал настоящий OpenAI-сервер с
конфликтующим портом; (6) «кодинг-валидация» заявлялась без M6. Файл сохраняется как история.

## 1. Волны и ветки (порядок влития)

| Волна | Ветки | Гейт волны |
|---|---|---|
| 0 | `chore/merge-phase2` | все CI-джобы зелёные на `main` |
| A | `w1/bench-harness`, `w1/gateway-telemetry` | M1-гейт + baseline в `docs/multinode-bench.md` |
| B | `w0/multinode-contract` | M2.1-гейт (proto/migrations/openapi/single-инвариант) |
| C | `w2/server-parallel-groups`, `w2/worker-multinode-runtime`, `w1/network-topology` | M2-гейт, M3-гейт |
| D | `w3/parity-evidence`, `w2/cache-aware-routing` | M4-гейт, M5-гейт |
| E | `w3/coding-validation` | M6-гейт |
| F | `w3/fault-and-soak`, `w4/diagnostics`, `w3/frontend-parallel` | M7-гейт |
| G | `w4/usage-metering`, `w4/autoscaling` | M8-гейт |
| H | `w5/release-hardening` | M9-гейт, тег `v1.0` |

`w4/usage-metering` — та же работа, что `plan-05-usage-metering.md`;
`w4/autoscaling` — Phase 2 из `plan-02-release-b-autoscaling.md` (масштабирование целыми группами).
Ветки волны A в программе `roadmap-program-03-06.md` исполняются после 04/03/05.

---

## M0 — База (ветка `chore/merge-phase2`)

1. `git merge --no-ff phase2-replicas-autoscaling` в `main`; конфликт-резолв в пользу `main`
   как источника правды; проверить, что `services/scaling.py`, `api/deployments.py`,
   `frontend/src/pages/InstancesPage.tsx` после мержа консистентны.
2. Закоммитить доки роадмапа: `docs/roadmap/06-distributed-inference.md` (untracked!) + правки
   `docs/roadmap/02-replicas-autoscaling.md`, `docs/roadmap/README.md` — это источник требований
   для всего плана (фактический `git status --short` от 2026-10-13 зафиксирован в
   `roadmap-program-03-06.md` §4.1).
3. Удалить из корня мусор ревью (проверено, что он лежит в корне): `pr2.diff`,
   `pr2_group4.diff`, `pr_group5.diff` (три файла по 175.4 KB / 4382 строки с идентичными хунками),
   `.pr_files.json` (13 KB), `REVIEW_REPORT.md` (7.9 KB) и `DISTRIBUTED_INFERENCE_PLAN.md`
   (дубль отклонённого `multi-node-mvp-plan.md`, сохранённый закрытым planning-агентом 2026-10-13).
   `AGENTS.md` закоммитить (репо-правила нужны; роадмап вопрос оставлял открытым — решение:
   оставить, убрав из него ссылки на процесс ревью).
4. `.gitignore` += `artifacts/` (прогонные JSONL `parity-*.jsonl`, `bench-out/`, `soak-*.jsonl`).
5. Гейт: `bash scripts/gen-proto.sh && git diff --exit-code`; `cd server && uv run python
   ../scripts/export-openapi.py --check`; `bash scripts/check-migrations.sh` (Postgres);
   backend-matrix (`ruff`/`pyright`/`pytest` в server и worker); `npm run lint`/`npx tsc --noEmit`/
   `npx vitest run`; e2e-стек (`docker compose -f docker-compose.yml -f docker-compose.mock.yml up
   -d --build postgres server worker` → `npx playwright test`).

Артефакт M0: закоммиченный `main` без мусора в корне; запись в `evidence-plan-06-distributed.md`.

---

## M1 — Измерения (волны A; `w1/bench-harness`, `w1/gateway-telemetry`)

### M1.1 `w1/bench-harness` (владеет `bench/`, `scripts/bench.sh`, `docs/multinode-bench.md`)

Новый **корневой** uv-проект `bench/` (не внутри `server/`): `pyproject.toml`, `__init__.py`,
`__main__.py`, `client.py`, `scenarios.py`, `metrics.py`, `report.py`, `trace.py`. Зависимости —
httpx (уже есть в проекте server; отдельный uv-проект с `httpx>=0.27`).

- `client.py`: `async def chat(client, base_url, key, model_name, messages, stream=True,
  max_tokens=128) -> RequestResult`; SSE по строкам `data: `; `RequestResult`:
  `status, ttft_ms, e2e_ms, itl_ms: list[float], prompt_tokens, completion_tokens, stream_error`.
  TTFT — первый content-чанк; ITL — интервалы между content-чанками; usage — из последнего чанка.
- `scenarios.py` (все `temperature: 0`, фиксированные промпты): `single` (concurrency sweep
  1/4/8/16), `agentic-loop` (8 итераций, PREFIX ≈4k токенов seed 42 + «итерация i»),
  `long-context` (32k **и** 128k, seed 7), `ttft-only` (короткий выход, max_tokens 1).
- `metrics.py`: `pct(values, p)` (линейная интерполяция), `tps(...)`.
- `report.py`: markdown-таблица p50/p95/p99 TTFT/e2e, mean tokens/s, ITL p95, число ошибок.
- `trace.py`: запись трассы в JSONL (`ts, method, path, body`) — вход для `--replay` (использует
  `plan-02-release-b-autoscaling.md` §5).
- CLI: `python -m bench --api URL --api-key KEY --model-id UUID --scenario {single|agentic-loop|
  long-context|ttft-only} --conc N --repeats N --warmup N --duration SEC --mode {gateway|direct}
  [--direct-base URL] [--replay FILE] --out artifacts/bench-<scenario>-<ts>.jsonl`.
  `--repeats` и `--warmup` обязательны (роадмап M1). `--mode direct` — тот же клиент на
  `instance.port`, минуя gateway (измерение накладных расходов прокси).
- `scripts/bench.sh` — тонкая обёртка: проверка env и `uv run --project bench python -m bench "$@"`.
- `docs/multinode-bench.md` — baseline-числа M0/M1: single-host и 2×DP, с командами прогона и
  явной пометкой mock/железо.
- Тесты (в корневом `bench/tests/`, запускаются джобом `backend` после добавления матрицы
  `bench` в CI): `test_pct_interpolates`, `test_hist_p95_helpers`, `test_client_parses_mock_sse`
  (httpx MockTransport), `test_scenario_agentic_prefix_grows`, `test_trace_roundtrip`.

Гейт M1.1: три прогона подряд → разброс p50 ≤ 5 % (числа и команды в `docs/multinode-bench.md`);
на mock-стеке таблица печатается.

### M1.2 `w1/gateway-telemetry` (владеет `api/gateway.py` — observe-точки, `services/metrics.py`,
`deploy/grafana/dashboards/gateway.json`)

- `services/metrics.py` — новые, **вне** `_ALL_GAUGES` (`:51-58`): `inferna_ttft_seconds`
  (Histogram, labels `model,engine,parallelism`), `inferna_inter_token_seconds` (то же),
  `inferna_output_tps` (то же). Существующие `inferna_requests_total`,
  `inferna_time_to_first_byte_seconds`, `inferna_tokens_total` не ломать.
- `api/gateway.py` `_relay`/`_proxy`: TTFT = время первого **content**-чанка (часы `time.perf_counter()`;
  чанк с одним `role` не считается), ITL — интервалы между content-чанками, TPS = `completion_tokens /
  (e2e - ttft)`; labels `engine` из `instance.engine`, `parallelism` из `instance.parallel_mode or
  "single"` (до M2.1 — литерал `"single"`).
  **Заголовок `X-Inferna-TTFT-Ms` не реализован сознательно**: gateway стримит, поэтому на момент
  отправки заголовков ответа TTFT ещё неизвестен, а HTTP-трейлеры OpenAI-клиенты не читают. Метрика —
  единственный честный канал; если заголовок понадобится, его можно отдавать только для
  не-стриминговых запросов (ценой буферизации ответа) — отдельное решение, не в этом срезе.
- `deploy/grafana/dashboards/gateway.json` — панели TTFT p50/p95, ITL p95, tokens/s по
  `model`/`parallelism` (uid дашборда не менять).
- Тест: `server/tests/test_gateway_telemetry.py::test_streaming_observes_ttft_itl_tps` (MockTransport
  со SSE из двух content-чанков → ненулевые значения в `REGISTRY`), `::test_ttft_header_present`.

Гейт M1.2: значения в `/metrics` ненулевые на моковом SSE; Grafana-панели не пустые на mock-стенде.

---

## M2 — Многонодовая модель

### M2.1 `w0/multinode-contract` (волна B; единственный писатель proto+alembic)

1. `proto/cluster.proto`:
   ```proto
   message Parallelism { string mode = 1; int32 tensor_parallel = 2; int32 pipeline_parallel = 3;
                         int32 world_size = 4; }
   message Rendezvous  { string master_address = 1; int32 master_port = 2; int64 job_id = 3;
                         int32 node_rank = 4; int32 num_nodes = 5; repeated string peers = 6;
                         repeated string env = 7; }   // env: MASTER_ADDR/MASTER_PORT/VLLM_HOST_IP/NCCL_*
   message EngineConfig { /* 1..7 без изменений */
                          Parallelism parallelism = 8; Rendezvous rendezvous = 9;
                          repeated string env = 10; }
   ```
   `bash scripts/gen-proto.sh` + коммит стабов в обоих пакетах.
2. `models.py`: `ModelInstance += parallel_group_id (uuid, nullable, index), node_rank (int, nullable),
   world_size (int, nullable), parallel_mode (str(16), nullable), rendezvous (str(255), nullable)`;
   частичный уникальный индекс `uq_model_instances_group_rank (parallel_group_id, node_rank)` для
   строк с `parallel_group_id IS NOT NULL`; `Deployment += nodes_per_replica (int, default 1),
   parallel_mode (str(16), default "single")` + CHECK на значения (`single|dp|pp|tp|pd`).
   Ревизия: `cd server && uv run alembic revision -m "multinode contract"` поверх текущего head;
   `upgrade()` — `add_column` + `create_index(..., unique=True, postgresql_where=..., sqlite_where=...)`
   (образец частичного индекса: `models.py:217-231`), SQLite-ветка через
   `batch_alter_table(recreate="always")` (образец `b4d7e91c2a58`), полный `downgrade()`.
3. `server/inferna_server/config.py` + `.env.example`: `INFERNA_RENDEZVOUS_PORT_RANGE_START=8110`,
   `INFERNA_RENDEZVOUS_PORT_RANGE_END=8199` (согласовано с роадмап §9.1).
4. `schemas.py`: `DeploymentOut += nodes_per_replica, parallel_mode`; `InstanceOut +=
   parallel_group_id, node_rank, world_size, parallel_mode, rendezvous` (аддитивно);
   `export-openapi.py` + коммит.

Гейт M2.1: `bash scripts/gen-proto.sh && git diff --exit-code`; `bash scripts/check-migrations.sh`;
`uv run ruff check . && uv run pyright && uv run pytest`; `export-openapi.py --check`;
**поведение `single` байт-в-байт неизменно** (`server/tests/test_instances.py`,
`test_deployments_api.py`, `worker/tests/test_engine_flags.py` зелёные без правок).

### M2.2 — две ветки параллельно от tip `w0`

**`w2/server-parallel-groups`** (владеет `services/scheduler.py`, `services/scaling.py`,
`services/workers_svc.py`, `api/instances.py`, `api/deployments.py`, `schemas.py`,
`docs/openapi.yaml`, и **только ранг-фильтром** в `api/gateway.py`):

- `scheduler.py` — `async def allocate_parallel_replicas(db, cluster_id, vram_required_mb, engine,
  nodes_per_replica: int, *, used: set[tuple[uuid.UUID, int]] | None = None) -> list[tuple[Worker,
  list[int], int, int]]` (worker, gpu_indexes, instance_port, rendezvous_port): N хостов с
  anti-affinity (переиспользовать Pass-логику `allocate_replicas:139-199`), N instance-портов
  (`_alloc_port`), N rendezvous-портов из нового диапазона, election rank 0 = хост с непустым
  `Worker.address` (`address` приходит из Register, `proto:17`). Порядок блокировок не менять
  (`deployment → workers`, `_connected_workers` с `FOR UPDATE`).
- `api/instances.py` — `DeployRequest += nodes_per_replica: int = 1 (ge=1, le=8), parallel_mode:
  Literal["single","dp","pp","tp","pd"] = "single"`; при `nodes_per_replica > 1` и `gpu_selection
  != "auto"` → 400 `"manual gpu selection is not supported for multi-node deploy"`;
  создаётся одна `Deployment` и N `ModelInstance` с общим `parallel_group_id`, `node_rank` =
  позиция, `world_size = nodes_per_replica`, `parallel_mode`, `rendezvous =
  f"{master_host}:{rendezvous_port}"` (**только в колонке `rendezvous`; `error_detail` не
  используется**), порт инстанса — только у rank 0, остальным `port=NULL`.
- `services/workers_svc.py:244-256` — в `EngineConfig` для группы: `parallelism =
  Parallelism(mode=..., world_size=..., tensor_parallel=len(gpu_indexes), pipeline_parallel=...)`,
  `rendezvous = Rendezvous(master_address=..., master_port=..., node_rank=..., num_nodes=...,
  env=[f"MASTER_ADDR={...}", f"MASTER_PORT={...}", f"VLLM_HOST_IP={advertise}"])`. `error_detail`
  остаётся диагностическим и больше ничем.
- `api/gateway.py` — **минимальный ранг-фильтр** (нужен для гейта M2): после `rows = ...` добавить
  `rows = [i for i in rows if i.parallel_group_id is None or i.node_rank == 0]`. Обоснование
  отклонения от §7 (где выбор инстанса принадлежит `w2/cache-aware-routing`): гейт M2 требует
  «gateway никогда не отправляет трафик на rank ≠ 0», а M5 идёт позже; при вливании M5 фильтр
  переезжает в скоринг `prefix_cache` и остаётся первым условием.
- `services/scaling.py` — масштабирование целыми группами (см. `plan-02-release-b-autoscaling.md`
  §6): единица — группа, scale-out создаёт группу целиком, scale-in останавливает все ранги.
- `docs/openapi.yaml` — регенерация в этой же ветке.

**`w2/worker-multinode-runtime`** (владеет `worker/inferna_worker/**`):

- `engines/base.py` — `@dataclass(slots=True) class MultiNodeSpec: mode; world_size; node_rank;
  master_host; master_port`, `def build_command(config: EngineConfig, spec: MultiNodeSpec | None =
  None, serve_port: int = CONTAINER_PORT) -> list[str]`; при `spec is None` — байт-в-байт текущее
  поведение (тесты `worker/tests/test_engine_flags.py` не меняются). Точные argv:
  - vLLM `pp`, rank 0: `--port <serve_port>` (host-порт инстанса), `--tensor-parallel-size
    <len(gpu_indexes)>`, `--pipeline-parallel-size <world>`, `--nnodes <world>`, `--node-rank 0`,
    `--master-addr <master_host>`, `--master-port <master_port>`, затем профиль-флаги;
  - vLLM rank > 0: то же без `--port` (API только на голове);
  - vLLM `tp`: `--tensor-parallel-size = len(gpu_indexes) * world`, `--distributed-executor-backend
    mp`, те же `--nnodes/--node-rank/--master-addr/--master-port`;
  - SGLang (оба режима): `SGLANG_BASE` + `--nnodes <world> --node-rank <rank> --dist-init-addr
    <master_host>:<master_port>` + `--pipeline-parallel-size <world>` (pp) или `--tp
    <len(gpu_indexes)*world>` (tp), `--port <serve_port>` только на rank 0.
  **Обязательное первое действие ветки**: `docker run --rm vllm/vllm-openai:v0.8.5 vllm serve
  --help 2>&1 | grep -E "nnodes|node-rank|master-addr|master-port|distributed-executor-backend"` и
  аналогично для SGLang-образа; если флагов нет — записать блокер в
  `evidence-plan-06-distributed.md` и выполнить mock-часть, живую валидацию пометить
  `BLOCKED: <причина>`. Точный argv закрепляется тестами
  (`test_vllm_multinode_pp_rank0_and_rank1`, `test_vllm_multinode_tp_sets_backend`,
  `test_sglang_multinode_uses_dist_init_addr`).
- `engines/manager.py`:
  - адрес/интерфейс: `multi_node_env(spec, advertise_host, netif) -> dict[str,str]` в `base.py`
    (`NCCL_SOCKET_IFNAME`, `VLLM_HOST_IP`) + `Rendezvous.env` из proto (весь список);
  - сеть: для мультинодовых контейнеров `network_mode="host"` **и** `ports=None`
    (docker-py бросает `ValueError` при обоих — передавать только `network_mode`); одна строка
    комментария с причиной: NCCL устанавливает входящие соединения между рангами, NAT-bridge их
    ломает;
  - готовность: rank 0 — существующий health-probe `/v1/models`; rank > 0 — `state="running"`
    после того, как docker-статус `"running"` держится ≥ `INFERNA_MN_RANK_READY_SECONDS`
    (default 20.0) в ветке `_health_probe`, до этого `starting`;
  - порядок остановки: сначала rank > 0, затем голова (иначе голова висит на ожидании пира);
    `_stop_sync`/`_delete_sync` дополняются сортировкой по `node_rank` (спека приходит из
    `EngineConfig.rendezvous.node_rank`).
  - mock: `INFERNA_MOCK_MULTINODE` не нужен как отдельный флаг — mock-ветка читает ранги из
    `EngineConfig.rendezvous` (rank 0 → `starting → running` по `MOCK_START_DELAY_SECONDS`,
    rank > 0 → `running` после той же задержки, без HTTP и без сети). Никакого OpenAI-сервера в
    воркере не поднимается (гейт M2 — lifecycle, не чат).
- `config.py` + `.env.example`: `advertise_host` (`INFERNA_WORKER_ADVERTISE_HOST`, default
  `platform.node()`), `netif` (`INFERNA_WORKER_NETIF`, default `eth0`),
  `mn_rank_ready_seconds` (`INFERNA_MN_RANK_READY_SECONDS`, default 20.0).
- `Dockerfile` worker: ничего не требуется, если host-сеть доступна из контейнера; проверить и
  записать вывод.

**`w1/network-topology`** (волна C, файлы не пересекаются):

- `docker-compose.multinode.yml` — 3 воркера на одной машине: разные `INFERNA_WORKER_NAME`,
  **разные** `INFERNA_WORKER_ADDRESS`, разные порт-диапазоны (`INFERNA_INSTANCE_PORT_RANGE_START/END`
  per worker — при необходимости добавить настройку в `config.py`, согласовав с шагом M2.1),
  разные `INFERNA_MODELS_DIR`, публикация портов, разные gRPC-клиентские имена. Устраняет
  коллизии, которые делают 3 воркера на одном хосте невозможными сегодня (единый
  `INFERNA_WORKER_ADDRESS`, общий docker.sock, единый пул 8010–8100).
- `scripts/netem.sh` — профили `dc-0.1ms`, `dc-2ms`, `wan-25ms-1%` (`tc netem` между воркерами),
  с проверкой `ping`/`iperf3`.
- `scripts/wan-envelope.sh` — `iperf3` + точечные замеры «RTT × полоса → обмен на стадию/токен».
- `docs/network-matrix.md` — порт-матрица как контракт: control `9091`, instance `8010–8100`
  (на воркера — свой поддиапазон), rendezvous `8110–8199`, NCCL/ephemeral `20000–20100`,
  NIXL `56000–56100`, + что открывать на файрволе. Порт-матрицу также добавить в
  `docs/architecture.md` (владелец этого файла — `w3/parity-evidence`, M4; здесь — черновик в
  `docs/network-matrix.md`, чтобы не пересекаться).

Гейт M2.2/M2: mock e2e `deploy(nodes_per_replica=2, parallel_mode=pp) → starting → running
(оба ранга) → stop → delete` зелёный; `server/tests/test_parallel_groups.py`,
`worker/tests/test_manager_multinode.py` (fake docker: `ports is None` + `network_mode == "host"`),
`server/tests/test_gateway_rank0_only.py` (из двух живых рангов выбирается только rank 0,
MockTransport). Железо: 2 GPU-хоста поднимают multi-node vLLM и отвечают через `/v1`
(`scripts/parity.sh --mode multi` из M4); если хостов нет —
`BLOCKED: no two-GPU-host environment available` в evidence, ветка не останавливается.

---

## M3 — Сеть и границы (см. `w1/network-topology` выше)

Гейт M3: три воркера регистрируются одновременно без коллизий портов; netem проверяемо меняет RTT
(приложены замеры `ping`/`iperf3`); envelope-таблица записана в `docs/network-matrix.md`.

---

## M4 — Вердикт паритета (`w3/parity-evidence`, волна D)

- `scripts/parity.sh` — единственный вход: `--mode {smoke|single|multi|full}`; конфигурации
  `1×8` (baseline), `2×4`, `4×2`; режимы `single|dp|pp|tp` (+ `pd` экспериментально) × сетевые
  профили M3; на ячейку — прогон `bench` (`--mode gateway` и `--direct`) и запись TTFT p50/p95,
  ITL, tokens/s @conc, коэффициенты `R_tps`/`R_p95` к baseline, вердикт по §5. Вывод: JSONL
  (`artifacts/parity-<ts>.jsonl`) + markdown-таблица в stdout; exit 0/1/2 (2 = нет стенда/предусловий).
  mock-числа помечать явно (`"env": "mock"`).
- `docs/multinode-parity.md` — итоговый отчёт: матрица чисел, раздел «граница применимости»
  («непригоден при RTT > X ms»), приложенные сырые JSONL, явная пометка mock/железо.
- Обновить `docs/architecture.md` (раздел Multi-node + порт-матрица из M3) и
  `docs/roadmap/README.md` (статус направления 7).

Гейт M4: одна команда воспроизводит весь отчёт; ни одной ячейки без измерения; mock помечены.

---

## M5 — Routing по префикс-кэшу (`w2/cache-aware-routing`, волна D; владеет выбором инстанса в
`api/gateway.py` и одноимённой метрикой в `services/metrics.py`)

- `services/prefix_cache.py` — нормализация префикса промпта, rolling-hash блоков (~128 токенов),
  карта «резидентные префиксы по инстансам» из счётчиков prefix-cache движка (парсить из ответов /
  KV-событий; на первом шаге — из usage-полей ответа `num_cached_tokens`, если движок отдаёт).
- `api/gateway.py` — скоринг: максимум совпавшего префикса → tie-break существующим least-loaded +
  LRU; **первым условием остаётся ранг-фильтр rank 0** (из M2.2). Инвариант «нет `await` внутри
  `_active_lock`» сохранить. Fallback: горячая реплика недоступна → обычный least-loaded.
- `services/metrics.py` — `inferna_prefix_cache_hit_ratio{model}` (gauge; вне `_ALL_GAUGES`).
- Тесты: `server/tests/test_prefix_cache.py::test_scoring_prefers_matching_prefix`,
  `::test_tie_break_least_loaded`, `::test_fallback_when_hot_replica_down`,
  `::test_rank0_filter_is_first_condition`.

Гейт M5: на 2 репликах повторные промпты агентного цикла в ≥ 90 % случаев идут в ту же реплику;
TTFT повторного ≤ 60 % холодного; при убийстве реплики поведение не хуже текущего.

---

## M6 — Кодинг-валидация (`w3/coding-validation`, волна E)

- `coding/` — фикс-набор 10–15 задач на тестовом репозитории: генерация функции, починка падающего
  теста, рефакторинг, правка по тексту ошибки; каждая задача — промпт + проверка результата
  (pytest-скрипт или текстовый ассерт), детерминированные входы.
- `scripts/coding-validation.sh` — прогон через `/v1`: клиент — aider/Continue (если установлен)
  и минимальный воспроизводимый агентный loop на OpenAI SDK; запись на задачу: TTFT, ITL, tok/s,
  число tool-итераций, успех/провал, prefix-cache hit; JSONL в `artifacts/`.
- `docs/coding-validation.md` — multi-node vs single-host baseline на той же модели и том же числе
  GPU; вывод «пригодно/непригодно» по §5.
- Регресс: повторный прогон в CI на mock (сокращённый набор) и по расписанию на железе
  (workflow по аналогии с `e2e`-джобом; добавить в `.github/workflows/ci.yml` джоб
  `coding-validation-mock`).

Гейт M6: `scripts/coding-validation.sh` зелёный на реальном железе; пороги §5 выполнены; в отчёте
сырые JSONL, ни одной пустой ячейки.

---

## M7 — Отказы и эксплуатация (волна F)

**`w3/fault-and-soak`** (владеет `scripts/soak-multinode.sh`, fault-тесты):
сценарии — убийство rank 0 mid-stream, потеря пира rank 1→2, рестарт воркера с живым контейнером,
netem loss 5 % mid-generation, рестарт сервера во время деплоя. Ожидания: предсказуемая ошибка
клиенту, остальные реплики живы, осиротевших контейнеров нет (`labels`-adoption уже есть,
`manager.adopt_existing:69-134`). `scripts/soak-multinode.sh` (+ `scripts/soak_multinode.py`) — 24 ч с ротацией сбоев, инварианты
проверяются каждый цикл (409/500, группа не остановилась, остались ранги — провал). Матрица
сбоев и ожиданий — `docs/fault-matrix.md`; smoke-режим `--cycles N` работает и без Docker.

**`w4/diagnostics`** (владеет `services/logs.py`, `api/instances.py` — логи, `api/diagnostics.py`):
per-rank логи поверх направления `04` (каждый ранг — своя строка `ModelInstance`, поэтому
`ModelInstance.log_tail` + `GET /api/v1/model-instances/{id}/logs` уже дают логи по рангам) и
он-деманд обновление внутри pull-модели: `?live=true` ждёт следующий Sync (до 12 с) и отдаёт
`stale=true`, если не дождался, `?lines=N` (1..1000) режет хвост.

**Отклонение от первой редакции этого пункта**: `rpc GetLogs` (server → worker) **не делаем**.
Он требует входящего доступа к GPU-хостам и противоречит инварианту §3.1 (control plane
pull-based) и порт-матрице M3, где явно записано «worker → server, входящий доступ не нужен»
(`docs/network-matrix.md`). Per-rank логи приходят с Sync, а `?live=true` — это ожидание
следующего Sync, а не RPC.

`/readyz` уже проверяет БД, актуальность миграций и готовность gRPC (`main.py:57-86`,
`readiness_loop`) — проверено чтением, дублировать не нужно.

Lifecycle-события (история переходов) отложены: под них нужна отдельная таблица, и ни один гейт
их не требует; причина падения ранга уже видна через `error_detail` + логи.

**`w3/frontend-parallel`** (владеет `frontend/**`):
в `/instances` группа отображает узлы/ранги/rendezvous; Endpoint-модал только у головы
(`node_rank == 0`); деплой-диалог получает `nodes per replica` и режим (`single|dp|pp|tp|pd`);
e2e на mock: `frontend/e2e/deploy-multinode.spec.ts` (deploy с `nodes_per_replica=2` → бейдж ранга,
rank 0 с endpoint, rank 1 без).

Гейт M7: fault-матрица пройдена и записана; soak 24 ч зелёный; причина падения ранга видна в UI
без SSH.

---

## M8 — Экономика и упругость (волна G)

- `w4/usage-metering` — `plan-05-usage-metering.md` целиком (UsageRecord, event из gateway,
  агрегатор, `/api/v1/usage`, UI-страница, Grafana, квоты, `scripts/usage-reconcile.py`), плюс
  M8-требование: агрегаты разбиваются и по **ключу**, и по пользователю, и по модели (в плане 05
  это `api_key_id` в `UsageRecord`). Квоты и агрегатор уже доставлены в волне A планом 05 — в этой
  ветке остаются сверка с gateway-метриками (`scripts/usage-reconcile.py` в CI/на расписании) и
  любые расхождения, найденные на multi-node стенде.
- `w4/autoscaling` — `plan-02-release-b-autoscaling.md` §6: масштабирование **только целыми
  параллельными группами**; отдельный ранг не является единицей масштабирования.
- `main.py` — фоновые циклы: usage-flush и autoscale (в планах 05/02 они добавляются как 4-я и
  5-я задачи lifespan).

Гейт M8: агрегаты сверяются с `inferna_requests_total`/`inferna_tokens_total`
(`scripts/usage-reconcile.py`, расхождение в допуске); политика проходит replay и не создаёт
осцилляций; scale-out не оставляет «половин» групп.

---

## M9 — Release v1.0 (`w5/release-hardening`, волна H)

- TLS/mTLS на gRPC (`9091`): сегодня `grpc_server.py:56` — `add_insecure_port`; добавить
  `INFERNA_GRPC_TLS_CERT`/`KEY`/`CA` + `server.add_secure_port` при заданных, иначе —
  задокументированная приватная граница с чек-листом файрвола (роадмап допускает оба варианта).
- Версионирование: `server/inferna_server/version.py` (`SERVER_VERSION`, `PROTOCOL_VERSION`,
  `MIN_WORKER_VERSION`) и `worker/inferna_worker/version.py`; гейт совместимости в
  `services/workers_svc.register_worker:61-83` **покрыть тестами** (сейчас их нет:
  `test_unsupported_protocol_version`, `test_worker_version_too_old`), поднять версии при
  изменениях контракта M2/M7.
- `docs/engine-matrix.md` — заполнить nvidia-ячейки по `scripts/hardware-smoke.sh` (все сейчас
  `unverified`/`—`); `docs/phase0-validation.md` — заполнить все `Observed: _TBD` и
  `Soak result` (включая multi-node soak из M7).
- `scripts/check-migrations.sh` — `PREVIOUS_RELEASE_HEAD` поднять на предыдущий релизный head
  (сейчас `3b491ceedb36`, уже отстал от реального head), прогнать upgrade-from-previous-release
  с legacy-данными.
- `CHANGELOG.md` (нет в корне) + обновить `README.md` (инструкция воспроизведения
  `scripts/parity.sh` и `scripts/coding-validation.sh` на чистом стенде); тег `v1.0`.
- Финальный прогон всех CI-джобов (команды — как в `roadmap-program-03-06.md` §4.1 п.4 и
  `.github/workflows/ci.yml`).

Гейт M9: чек-лист релиза пройден; `docs/multinode-parity.md` и `docs/coding-validation.md`
воспроизводимы по README.

---

## Инварианты (не нарушать; роадмап §3)

1. Control plane pull-based, один сервер, без брокеров/очередей.
2. `Worker.address` — без порта; rendezvous живёт **только** в `ModelInstance.rendezvous`
   (никаких `error_detail`-хаков).
3. Трафик gateway — только на rank 0 (фильтр в M2.2 → перенос в M5, остаётся первым условием).
4. Топология задаётся декларативно; воркер не принимает решений о топологии.
5. `INFERNA_MOCK_ENGINE` обязан поддерживать multi-node (иначе CI/e2e непроверяемы).
6. `parallel_mode = "single"` работает ровно как сегодня (байт-в-байт).

## Port-контракт (§9.1) и SSRF

Любая маршрутизация gateway/`Worker.address` проходит через `services/upstream_guard.py`
(`resolve_and_validate` — пиннинг IP против DNS-rebinding); политика зависит от `INFERNA_ENV` и
`gateway_upstream_allowlist_entries`. Для multi-node в dev-compose адреса воркеров добавляются в
allowlist/`extra_hosts` (как сейчас `host.docker.internal`).

## Hard gates и железо

Требуют реальных GPU-хостов: M2 (железо-часть), M3 (3 хоста), M4, M6, M9 (engine-matrix,
phase0). Отсутствие железа не останавливает волну: пишется
`BLOCKED: no two-GPU-host environment available`, продолжаются mock-гейты, живые прогоны —
отдельной сессией.

## Evidence Artifacts

`evidence-plan-06-distributed.md` рядом с планом (в `docs/roadmap/plans/`, коммитится): по каждой вехе — команда, сокращённый вывод,
результат, дата, статус гейта (`PASS`/`BLOCKED: причина`/`FAIL`). Прогонные артефакты —
`artifacts/**` (git-ignored, M0). Коммитируемые отчёты — только те, что требует роадмап:
`docs/multinode-bench.md`, `docs/multinode-parity.md`, `docs/coding-validation.md`,
`docs/network-matrix.md`, `docs/architecture.md`, `docs/roadmap/README.md`, `docs/engine-matrix.md`,
`docs/phase0-validation.md`, `CHANGELOG.md`, `README.md`.