"""HTTP API агента: uvicorn agent.api:app --host 0.0.0.0 --port 8000"""
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import semantic
from .core import Agent

_agent = None
_lock = threading.Lock()   # одно соединение и общий журнал SQL на процесс


def agent():
    global _agent
    if _agent is None or _agent.db.conn.closed:
        _agent = Agent()
    return _agent


@asynccontextmanager
async def lifespan(_app):
    with _lock:   # прогрев модели эмбеддингов, чтобы первый запрос не ждал загрузки
        semantic.search(agent().db, "прогрев", k=1)
    yield


app = FastAPI(title="AI-агент анализа госзакупок РК", version="1.0", lifespan=lifespan)


class Question(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000,
                          examples=["Найди закупки с отклонением цены > 30% от средневзвешенной по ЕНС ТРУ"])
    with_sql: bool = False


@app.get("/health")
def health():
    with _lock:
        try:
            agent().db.one("SELECT 1 AS ok")
        except Exception as e:
            raise HTTPException(503, f"База недоступна: {e}")
    return {"status": "ok"}


@app.post("/ask")
def ask(q: Question):
    with _lock:
        res = agent().ask(q.question, with_sql=q.with_sql)
    p, r = res["params"], res["result"]
    return {
        "question": q.question,
        "type": p.qtype,
        "lang": p.lang,
        "confidence": r.confidence,
        "answer_markdown": res["text"],
        "params": {"bins": p.bins, "years": p.period, "enstru": p.enstru, "regions": p.regions,
                   "lot_ref": p.lot_ref, "threshold": p.threshold},
        "sql": res["sql"] if q.with_sql else None,
    }
