# Общий образ для ETL и агента. Данные — на томе /data, секреты — через переменные окружения.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    TZ=Asia/Almaty \
    PG_HOST=db \
    PG_PORT=5432

WORKDIR /app
COPY requirements.txt .
# torch без CUDA
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.7.1 \
 && pip install -r requirements.txt

# модель эмбеддингов кладём в образ, чтобы работать без интернета
ENV HF_HOME=/app/models EMBED_MODEL=intfloat/multilingual-e5-small
RUN python -c "from sentence_transformers import SentenceTransformer as S; S('intfloat/multilingual-e5-small')"
ENV HF_HUB_OFFLINE=1

COPY agent/ agent/
COPY eval/ eval/
COPY db/ db/
COPY ingest.py market.py load.py update.py enrich.py ml_anomaly.py rag_index.py question_test.py subjects.json ./

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data/raw /data/raw_market \
 && chown -R app /app /data
USER 10001
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# по умолчанию — API агента; ETL: docker compose run --rm etl
CMD ["uvicorn", "agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
