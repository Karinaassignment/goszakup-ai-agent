"""Сравнение моделей эмбеддингов на eval/item_retrieval.ITEMS (векторы в памяти, пороги — на dev).

Запуск: python -m eval.compare_models [модель ...]
"""
import re
import sys
import time

import numpy as np
from sentence_transformers import SentenceTransformer

import rag_index
from agent import tools
from agent.db import DB
from agent.router import route
from eval.item_retrieval import ITEMS, question, score

MODELS = ["intfloat/multilingual-e5-small", "intfloat/multilingual-e5-base", "sentence-transformers/LaBSE"]
GRID = [(d, f) for d in (0.0, 0.01, 0.02, 0.03, 0.05) for f in (0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8, 0.82, 0.84, 0.86, 0.88)]


def prefixes(model):
    return ("query: ", "passage: ") if "e5" in model else ("", "")


def evaluate(found_by_id, rows, part):
    sel = [r for r in rows if r["part"] == part]
    ps, rs, fs, hits = [], [], [], 0
    for r in sel:
        found = found_by_id[r["id"]]
        p, rc, f = score(found, r["truth"])
        ps.append(p); rs.append(rc); fs.append(f); hits += bool(set(found) & set(r["truth"]))
    n = len(sel)
    return dict(f1=sum(fs) / n, precision=sum(ps) / n, recall=sum(rs) / n, hit=hits / n)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    db = DB()
    names = {r["code"]: r["name_ru"] for r in db.query("SELECT code, name_ru FROM ref.enstru")}
    with db.conn.cursor() as cur:
        docs = rag_index.build_docs(cur)
    codes = list(docs)
    rows = []
    for cid, part, phrase, pattern in ITEMS:
        p = route(question(phrase), {})
        rows.append(dict(id=cid, part=part, phrase=p.item_phrase,
                         truth=[c for c, n in names.items() if n and re.search(pattern, n)],
                         lex=tools.resolve_items(db, route(question(phrase), {}))))

    print(f"{'модель':<40} {'набор':<5} {'режим':<9} {'F1':>5} {'точн.':>6} {'полн.':>6} {'верный код':>11}")
    for name in sys.argv[1:] or MODELS:
        t = time.time()
        model = SentenceTransformer(name, device="cpu")
        qp, dp = prefixes(name)
        m = model.encode([dp + docs[c] for c in codes], batch_size=128, normalize_embeddings=True)
        qv = model.encode([qp + r["phrase"] for r in rows], normalize_embeddings=True)
        ranked = {}
        for r, q in zip(rows, qv):
            s = m @ q
            top = np.argsort(-s)[:30]
            ranked[r["id"]] = [(codes[i], float(s[i])) for i in top]

        def pick(delta, floor, hybrid):
            out = {}
            for r in rows:
                sem = ranked[r["id"]]
                chosen = [c for c, s in sem if s >= max(sem[0][1] - delta, floor)]
                out[r["id"]] = (r["lex"] or chosen) if hybrid else chosen
            return out

        for hybrid in (False, True):
            best = max(GRID, key=lambda df: evaluate(pick(*df, hybrid), rows, "dev")["f1"])
            for part in ("dev", "test"):
                res = evaluate(pick(*best, hybrid), rows, part)
                print(f"{name:<40} {part:<5} {'гибрид' if hybrid else 'RAG':<9} {res['f1']:>5.2f} {res['precision']:>6.2f} "
                      f"{res['recall']:>6.2f} {res['hit'] * 100:>10.0f}%   пороги {best}")
        print(f"   ({time.time() - t:.0f} с на индекс {len(codes)} кодов)")
    for part in ("dev", "test"):
        res = evaluate({r["id"]: r["lex"] for r in rows}, rows, part)
        print(f"{'лексический (текущий)':<40} {part:<5} {'—':<9} {res['f1']:>5.2f} {res['precision']:>6.2f} "
              f"{res['recall']:>6.2f} {res['hit'] * 100:>10.0f}%")


if __name__ == "__main__":
    main()
