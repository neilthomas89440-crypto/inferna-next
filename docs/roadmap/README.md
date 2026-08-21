# Roadmap

Актуализировано после аудита кодовой базы 20 августа 2026 года.

## Выбранный вектор

Inferna должна развиваться не как ещё одна панель запуска Docker-контейнеров, а как
надёжный self-hosted OpenAI-compatible inference control plane. Ближайшая продуктовая
цель: пользователь добавляет GPU-узел, разворачивает поддерживаемую модель и обращается
к ней через единый защищённый endpoint, не зная адресов воркеров и портов контейнеров.

Сейчас control plane, worker reconciliation, каталог и UI уже образуют хороший прототип.
Но трафик идёт в обход платформы, а реальный lifecycle ещё недостаточно надёжен для
эксплуатации. Поэтому gateway действительно является главным продуктовым направлением,
но сначала нужен короткий этап стабилизации. Реплики и autoscaling до появления
измеримого трафика преждевременны.

## Порядок работ

| Этап                            | Результат                                              | Зависит от | Условие завершения                                             |
| ------------------------------- | ------------------------------------------------------ | ---------- | -------------------------------------------------------------- |
| 0. Production readiness         | Предсказуемый lifecycle на реальном NVIDIA-узле        | —          | 24-часовой soak без потерянных команд и конфликтов ресурсов    |
| 1. Inference gateway            | Защищённый `/v1`, API keys, streaming, traffic metrics | этап 0     | OpenAI client стабильно работает только через gateway          |
| 2. Диагностика                  | Логи, события lifecycle, readiness, понятные ошибки    | этап 0     | Причина неудачного deploy видна без SSH                        |
| 3. Custom models                | Управляемый каталог Hugging Face моделей               | этапы 0, 2 | Произвольная совместимая модель запускается без изменения кода |
| 4. Deployments и ручные реплики | Группа экземпляров, scale N, failover                  | этапы 1, 2 | Потеря одной реплики не прерывает endpoint                     |
| 5. Usage и quotas               | История запросов/tokens по ключам и пользователям      | этап 1     | Агрегаты сверяются с gateway metrics                           |
| 6. Autoscaling                  | Масштабирование по измеренной нагрузке                 | этапы 4, 5 | Политика проходит replay реального traffic trace               |

Подробные планы существующих направлений:

- [Inference Gateway](01-inference-gateway.md)
- [Replicas + Autoscaling](02-replicas-autoscaling.md) — выполнять двумя отдельными
  релизами: сначала ручные реплики, autoscaling только после usage telemetry.
- [Custom Models](03-custom-models.md)
- [Instance Logs](04-instance-logs.md)
- [Usage Metering](05-usage-metering.md)

## Этап 0 — Production readiness

### P0: корректность и безопасность

1. Сделать allocation атомарным. Сейчас два параллельных deploy могут выбрать один
   GPU и один host port: scheduler сначала читает live instances, а запись происходит
   позже без блокировки и DB constraints. Для PostgreSQL нужна транзакционная модель
   резервирования и уникальность активного `(worker_id, port)`.
2. Исправить manual placement: выбранный worker обязан принадлежать `cluster_id` из
   запроса. Сейчас можно создать instance с cluster A и worker из cluster B.
3. Определить lifecycle как явный desired/observed state. Отдельные поля устранят
   смешение команды пользователя со статусом worker. Добавить restart/resume и retry
   failed deploy; идемпотентность команд закрепить sequence/generation id.
4. Не удалять все inference-контейнеры при каждом рестарте worker. Worker должен
   обнаруживать контейнеры по labels, восстанавливать состояние и удалять только
   подтверждённые orphan containers.
5. Закрыть production defaults: сервер не должен стартовать с дефолтными JWT,
   registration/admin secrets; gRPC требует TLS/mTLS либо документированную private
   network boundary; inference ports не публикуются наружу после появления gateway.
6. Убрать из gRPC INTERNAL ответы `str(exc)`: детали остаются в structured logs,
   клиент получает стабильный публичный код ошибки.

### P1: эксплуатационная надёжность

1. Добавить DB constraints: unique `(worker_id, gpu_index)`, unique worker identity
   внутри cluster, enum/check constraints для role/state/engine/profile, определённые
   `ondelete` правила для всех foreign keys.
2. Разделить `/healthz` (процесс жив) и `/readyz` (DB доступна, migrations актуальны,
   gRPC bind успешен). Ошибка миграции/seed должна делать сервер not-ready, а не оставлять
   внешне здоровый, но неработоспособный API.
3. Удерживать и корректно завершать ссылки на background tasks worker-а: apply batch и
   health probes. Добавить bounded concurrency, timeouts и итоговый observed error для
   любой неисполненной команды.
4. Проверить реальный NVIDIA happy path и failure matrix: image pull failure, gated
   model без token, OOM, занятый port, Docker restart, worker restart, server restart,
   network partition и повторная регистрация.
5. Сделать engine compatibility явными данными модели. Каталог содержит LLM,
   embedding, reranker, audio и multimodal модели, но UI сейчас разрешает любой из двух
   engines для любой категории.

### P2: качество поставки

1. Сделать Pyright обязательным в CI: сейчас `continue-on-error` скрывает регрессии.
2. Добавить migration check на пустой PostgreSQL и upgrade с предыдущей release schema.
3. Расширить e2e до фактического deploy в mock mode: `scheduled → starting → running →
stopped → deleted`. Текущий smoke только открывает и закрывает deploy dialog.
4. Зафиксировать поддерживаемую матрицу engine image × GPU vendor × model category.
   Версии image должны обновляться осознанно и проходить hardware smoke.
5. Добавить release versioning server/worker/protocol и проверку совместимости при
   регистрации worker.

## Что сознательно не делать сейчас

- Не начинать autoscaling до gateway metrics, deployment abstraction и реальных traces.
- Не добавлять Kubernetes, очередь, Redis или отдельный gateway service: текущий single
  server соответствует масштабу и продуктовой гипотезе.
- Не обещать «широкую поддержку GPU»: код реализует NVIDIA, AMD и mock. Сначала нужно
  подтвердить NVIDIA end-to-end, затем отдельно квалифицировать AMD.
- Не строить billing. Usage accounting и quotas достаточны до появления коммерческой
  модели продукта.
- Не расширять каталог количеством моделей, пока compatibility и hardware smoke не
  защищают one-click deploy от заведомо нерабочих комбинаций.

## Метрики прогресса продукта

- Time-to-first-token от чистого GPU host до первого ответа через `/v1`.
- Deploy success rate по поддерживаемой матрице.
- Recovery time после рестарта worker/server и сетевого разрыва.
- Доля ошибок deploy, диагностируемых из UI без SSH.
- Gateway availability и p95 latency overhead относительно прямого engine endpoint.

После этапов 0–2 проект можно честно называть self-hosted Model-as-a-Service. До этого
это качественный control-plane prototype, но не production inference platform.
