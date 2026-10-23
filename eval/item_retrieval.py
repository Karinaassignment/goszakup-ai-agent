"""Поиск кода ЕНС ТРУ по фразе: лексический vs RAG vs гибрид. Пороги подбираются только на dev.

Запуск: python -m eval.item_retrieval [--tune]
"""
import functools
import itertools
import re
import sys

from agent import semantic, tools
from agent.db import DB
from agent.router import route

# (id, набор, фраза, регулярное выражение эталонных названий ЕНС ТРУ)
ITEMS = [
    ("R1", "dev", "бумага для принтера", r"^Бумага для офисного"),
    ("R2", "test", "ноутбуки", r"^Ноутбук"),
    ("R3", "dev", "картриджи для принтера", r"^Картридж$"),
    ("R4", "test", "одноразовые шприцы", r"^Шприц"),
    ("R5", "dev", "медицинские перчатки", r"^Перчатки"),
    ("R6", "test", "охрана здания", r"^Услуги охраны"),
    ("R7", "dev", "уборка офиса", r"^Услуги по уборке зданий"),
    ("R8", "test", "бензин аи-92", r"^Бензин"),
    ("R9", "dev", "дизтопливо", r"^Топливо дизельное"),
    ("R10", "test", "мониторы для компьютера", r"^Монитор$"),
    ("R11", "dev", "шариковые ручки", r"^Ручка( канцелярская)?$"),
    ("R12", "test", "моторное масло", r"^Масло моторное"),
    ("R13", "dev", "автомобильные шины", r"^Шина$"),
    ("R14", "test", "мобильная связь", r"^Услуги сотовой связи"),
    ("R15", "dev", "доступ в интернет", r"^Услуги (по доступу к Интернету|мобильного интернета)"),
    ("R16", "test", "говядина", r"^Говядина"),
    ("R17", "dev", "офисные кресла", r"^Кресло"),
    ("R18", "test", "вывоз мусора", r"^Услуги по вывозу"),
    ("R19", "dev", "моющее средство для посуды", r"^Средство моющее"),
    ("R20", "test", "жидкое мыло", r"^Мыло"),
    ("K1", "test", "ноутбуктер", r"^Ноутбук"),
    ("K2", "dev", "принтерге арналған картридждер", r"^Картридж$"),
    ("K3", "test", "бір реттік шприцтер", r"^Шприц"),
    ("K4", "dev", "медициналық қолғаптар", r"^Перчатки"),
    ("K5", "test", "ғимаратты күзету қызметі", r"^Услуги охраны"),
    ("K6", "dev", "үй-жайларды жинау", r"^Услуги по уборке зданий"),
    ("K7", "test", "кеңсе қағазы", r"^Бумага для офисного"),
    ("K8", "dev", "мониторлар", r"^Монитор$"),
    ("K9", "test", "мотор майы", r"^Масло моторное"),
    ("K10", "dev", "автокөлік шиналары", r"^Шина$"),
    ("K11", "test", "ұялы байланыс қызметтері", r"^Услуги сотовой связи"),
    ("K12", "dev", "интернетке қол жеткізу", r"^Услуги (по доступу к Интернету|мобильного интернета)"),
    ("K13", "test", "сиыр еті", r"^Говядина"),
    ("K14", "dev", "қоқыс шығару", r"^Услуги по вывозу"),
    ("K15", "test", "жуғыш құрал", r"^Средство моющее"),
    ("K16", "dev", "сабын", r"^Мыло"),
    ("K17", "test", "дизель отыны", r"^Топливо дизельное"),
    ("K18", "dev", "кеңсе креслолары", r"^Кресло"),
]


def question(phrase):
    return f"{phrase} сатып алуды тап" if re.search("[әғқңөұүһі]", phrase) else f"Найди закупки: {phrase}"


def score(found, truth):
    found, truth = set(found), set(truth)
    hit = len(found & truth)
    p = hit / len(found) if found else 0.0
    r = hit / len(truth) if truth else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def lexical_before_rag(db, p):
    codes = tools._codes_for(db, p.item_terms) if p.item_terms else []
    if not codes and p.item_terms:
        useful = [w for w in p.item_terms if tools._codes_for(db, [w])]
        codes = tools._codes_for(db, useful) if useful else []
    return codes


def evaluate(db, names, mode, part=None):
    ps, rs, fs, hits, n = [], [], [], 0, 0
    per = []
    for cid, prt, phrase, pattern in ITEMS:
        if part and prt != part:
            continue
        truth = [c for c, nm in names.items() if nm and re.search(pattern, nm)]
        p = route(question(phrase), {})
        found = {"до RAG": lambda: lexical_before_rag(db, p),
                 "RAG": lambda: tools._semantic_codes(db, p),
                 "гибрид": lambda: tools.resolve_items(db, p)}[mode]()
        pr, rc, f = score(found, truth)
        ps.append(pr); rs.append(rc); fs.append(f); n += 1
        ok = bool(set(found) & set(truth))
        hits += ok
        per.append((cid, phrase, ok, len(found), p.item_source))
    return dict(f1=sum(fs) / n, precision=sum(ps) / n, recall=sum(rs) / n, hit=hits / n, per=per)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    db = DB()
    names = {r["code"]: r["name_ru"] for r in db.query("SELECT code, name_ru FROM ref.enstru")}
    semantic.search = functools.lru_cache(maxsize=None)(semantic.search.__wrapped__
                                                        if hasattr(semantic.search, "__wrapped__") else semantic.search)
    if "--tune" in sys.argv:
        grid = itertools.product((0.0, 0.01, 0.02, 0.03, 0.05), (0.80, 0.82, 0.84, 0.86), (0.0, 0.02, 0.03, 0.05, 0.08))
        best = max(grid, key=lambda g: (setattr(tools, "SEM_DELTA", g[0]), setattr(tools, "SEM_FLOOR", g[1]),
                                        setattr(tools, "SEM_HEAD", g[2]),
                                        evaluate(db, names, "гибрид", "dev")["f1"])[-1])
        tools.SEM_DELTA, tools.SEM_FLOOR, tools.SEM_HEAD = best
        print(f"Подобрано на dev: RAG_DELTA={best[0]}, RAG_FLOOR={best[1]}, RAG_HEAD_BONUS={best[2]}\n")
    print(f"Пороги: delta={tools.SEM_DELTA}, floor={tools.SEM_FLOOR}, надбавка за главное слово={tools.SEM_HEAD}\n")
    print(f"{'набор':<6} {'режим':<8} {'F1':>5} {'точн.':>6} {'полн.':>6} {'верный код':>11}")
    results = {}
    for part in ("dev", "test"):
        for mode in ("до RAG", "RAG", "гибрид"):
            m = evaluate(db, names, mode, part)
            results[(part, mode)] = m
            print(f"{part:<6} {mode:<8} {m['f1']:>5.2f} {m['precision']:>6.2f} {m['recall']:>6.2f} {m['hit'] * 100:>10.0f}%")
    print("\nTest по фразам (гибрид агента):")
    for cid, phrase, ok, n, src in results[("test", "гибрид")]["per"]:
        print(f"  {cid:<4} {'✅' if ok else '❌'} {phrase:<30} кодов {n:>3}  источник: {src}")


if __name__ == "__main__":
    main()
