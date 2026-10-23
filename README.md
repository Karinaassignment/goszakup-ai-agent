# AI-агент анализа государственных закупок Республики Казахстан

Аналитический агент по данным API OWS v3 (goszakup.gov.kz): 27 организаций, 2024–2026 годы. Отвечает на вопросы на русском и казахском языках — поиск, сравнение, аналитика, аномалии цены, справедливость цены (Fair Price), объёмы — проверяемыми ответами из шести разделов, с ID закупок и ссылками на портал.

## Документы (ТЗ п. 12)

1. [Архитектура](docs/01_architecture.md) — схема API → хранилище → аналитика → агент, выбор эндпоинтов, инкрементальное обновление, развёртывание
2. [Схема хранения данных](docs/02_data_schema.md) — слои raw / ref / core / mart / etl, модель данных, ETL и контроль качества
3. [Аналитика и метрики](docs/03_analytics_metrics.md) — Fair Price, аномалии, объёмы, ML, RAG, оценка точности
4. [Примеры ответов агента](docs/04_examples.md) — 13 ответов на русском и казахском, включая обязательные вопросы ТЗ п. 4
5. [Риски и ограничения](docs/05_risks_limitations.md)

## Ключевые результаты

| Показатель | Значение |
|---|---|
| Точность фактов (ТЗ п. 3.5, порог 85%) | 100% на основном наборе (30 вопросов, 118 фактов); на новых вопросах до доработки — 25,7% |
| Формат ответа ТЗ п. 8 | 45 из 45 ответов |
| Fair Price | 48,6% позиций, 79,7% суммы закупок |
| Актуальность данных | ежедневное инкрементальное обновление, ≤ 24 ч |
| Поиск товара по свободной формулировке (RU / KZ) | верный код ЕНС ТРУ: 37% → 100% на test (RAG + LLM) |

## Быстрый старт

```bash
# 1. Секреты: скопировать secret_key.env.example → secret_key.env, db.env.example → db.env и заполнить
docker compose up -d                                  # PostgreSQL + API агента
docker compose run --rm etl python ingest.py          # первичная выгрузка 27 организаций (часы)
docker compose run --rm etl python market.py          # рыночная выборка для Fair Price
docker compose run --rm etl python load.py            # загрузка, витрины, ML, RAG-индекс
docker compose run --rm etl                           # ежедневное обновление (update.py)

# необязательно: локальная LLM (GPU)
docker compose --profile llm up -d
docker exec goszakup_llm ollama pull qwen3:4b-instruct-2507-q4_K_M && docker compose restart agent
```

Вопрос агенту:

```bash
curl -X POST localhost:8000/ask -H "Content-Type: application/json" \
     -d '{"question": "Найди закупки с отклонением цены > 30% от средневзвешенной по ЕНС ТРУ"}'
python question_test.py "Беркут 2025 жылы қанша жұмсады"       # без Docker
python -m eval.run_eval                                        # оценка точности
```

Kubernetes — [k8s/README.md](k8s/README.md).

## Структура

```
agent/          AI-агент: router (тип и параметры), tools (SQL-инструменты), semantic (RAG), llm, render, api
db/init/        схема БД (выполняется при первом старте), db/transform.sql — пересборка core, db/migrations/
eval/           эталонные вопросы и оценка точности
k8s/            манифесты Kubernetes;  scheduler/ — расписание без контейнеров
ingest.py market.py update.py enrich.py   выгрузка из OWS v3
load.py ml_anomaly.py rag_index.py        загрузка, ML, RAG-индекс
```
