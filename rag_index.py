"""Векторы кодов ЕНС ТРУ для семантического поиска; пересчитываются только изменившиеся документы.

Запуск: python rag_index.py   (load.py вызывает run() сам)
"""
import hashlib
import os
import sys
import time

import psycopg2
from psycopg2.extras import execute_values

MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small")
BATCH = 256

DOCS = """
WITH names AS (
    SELECT enstru_code AS code, name_clean AS name FROM core.plan_point WHERE name_clean IS NOT NULL
    UNION ALL
    SELECT enstru_code, name_clean FROM core.contract_unit WHERE name_clean IS NOT NULL
), ranked AS (
    SELECT code, name, row_number() OVER (PARTITION BY code ORDER BY count(*) DESC, name) AS rn
    FROM names GROUP BY code, name
)
SELECT e.code, e.name_ru, e.name_kz,
       (SELECT string_agg(r.name, '; ' ORDER BY r.rn) FROM ranked r
        WHERE r.code = e.code AND r.rn <= 3 AND r.name <> lower(e.name_ru)) AS variants
FROM ref.enstru e
WHERE e.name_ru IS NOT NULL
"""


def build_docs(cur):
    """Название RU + KZ + до трёх частых формулировок из планов и договоров."""
    cur.execute(DOCS)
    docs = {}
    for code, name, name_kz, variants in cur.fetchall():
        parts = [name.strip()] + ([name_kz.strip()] if name_kz else []) + ([variants] if variants else [])
        docs[code] = "; ".join(parts)
    return docs


def run(conn):
    from sentence_transformers import SentenceTransformer

    cur = conn.cursor()
    docs = build_docs(cur)
    cur.execute("SELECT code, doc_hash FROM ref.enstru_embedding WHERE model = %s", (MODEL,))
    known = dict(cur.fetchall())
    todo = {c: d for c, d in docs.items()
            if known.get(c) != hashlib.md5(d.encode()).hexdigest()}
    if not todo:
        print(f"RAG: индекс актуален ({len(known)} кодов)")
        return
    t = time.time()
    model = SentenceTransformer(MODEL, device="cpu")
    codes = list(todo)
    # e5 ожидает префиксы passage:/query:
    vectors = model.encode([f"passage: {todo[c]}" for c in codes], batch_size=BATCH,
                           normalize_embeddings=True, show_progress_bar=False)
    rows = [(c, MODEL, todo[c], hashlib.md5(todo[c].encode()).hexdigest(), v.astype(float).round(6).tolist())
            for c, v in zip(codes, vectors)]
    execute_values(cur, """
        INSERT INTO ref.enstru_embedding (code, model, doc, doc_hash, emb) VALUES %s
        ON CONFLICT (code) DO UPDATE SET model = EXCLUDED.model, doc = EXCLUDED.doc,
            doc_hash = EXCLUDED.doc_hash, emb = EXCLUDED.emb, updated_at = now()""", rows, page_size=500)
    conn.commit()
    print(f"RAG: векторизовано {len(rows)} кодов за {time.time() - t:.0f} с ({MODEL})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    from load import DSN
    with psycopg2.connect(**DSN) as conn:
        run(conn)
