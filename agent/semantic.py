"""Семантический поиск кода ЕНС ТРУ по векторам из ref.enstru_embedding (строит rag_index.py)."""
import os
import threading

import numpy as np

MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small")
_lock = threading.Lock()
_model = None
_codes, _matrix = None, None


def available():
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _load(db):
    global _model, _codes, _matrix
    with _lock:
        if _matrix is None:
            with db.conn.cursor() as cur:   # мимо db.log: это не часть ответа
                cur.execute("SELECT code, emb FROM ref.enstru_embedding WHERE model = %s", (MODEL,))
                rows = cur.fetchall()
            _codes = [r[0] for r in rows]
            _matrix = np.array([r[1] for r in rows], dtype=np.float32) if rows else np.zeros((0, 384), np.float32)
        if _model is None:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL, device="cpu")


def search(db, phrase, k=30):
    """[(код, близость)] по убыванию; [] если нет модели или индекса."""
    if not phrase or not available():
        return []
    _load(db)
    if not len(_codes):
        return []
    q = _model.encode([f"query: {phrase}"], normalize_embeddings=True)[0].astype(np.float32)
    scores = _matrix @ q
    top = np.argsort(-scores)[:k]
    return [(_codes[i], float(scores[i])) for i in top]
