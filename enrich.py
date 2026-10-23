"""Догрузка новых полей (вид закупки, поставщик, KZ-названия ЕНС ТРУ) в уже скачанные договоры и планы.

Качаются только эти поля, load.py дописывает их в payload — полная перевыгрузка не нужна.
Запуск: python enrich.py && python load.py
"""
import json
import sys
import time

import ingest
from load import DATA

OUT = DATA / "raw" / "enrich"
LIMIT = 200


def fetch_all(entity, filt, fields):
    rows, after = [], None
    while True:
        args = "filter: {%s}, limit: %d" % (filt, LIMIT) + (", after: %d" % after if after else "")
        d = ingest.post("{ %s(%s) { id %s } }" % (entity, args, fields))
        page_rows = (d.get("data") or {}).get(entity) or []
        page = (d.get("extensions") or {}).get("pageInfo") or {}
        rows += page_rows
        after = page.get("lastId")
        if not page_rows or not page.get("hasNextPage") or after is None:
            return rows
        time.sleep(ingest.PAUSE)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [(f"Contract_{b}_{y}", "Contract", f'customerBin: "{b}", finYear: {y}', ingest.CONTRACT_EXTRA)
            for b in ingest.BINS for y in ingest.YEARS]
    jobs += [(f"Plans_{b}_{y}", "Plans", f'subjectBiin: "{b}", plnPointYear: {y}', ingest.PLANS_EXTRA)
             for b in ingest.BINS for y in ingest.YEARS]
    started, failed, total = time.time(), [], 0
    for i, (name, entity, filt, fields) in enumerate(jobs, 1):
        path = OUT / f"{name}.jsonl"
        if path.exists():
            continue
        try:
            rows = fetch_all(entity, filt, fields)
        except RuntimeError as e:
            print(f"ОШИБКА {name}: {e}")
            failed.append(name)
            continue
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(path)
        total += len(rows)
        if i % 20 == 0:
            print(f"[{i}/{len(jobs)}] записей {total}")
    print(f"Готово за {time.time() - started:.0f} с: {total} записей. Ошибки: {failed or 'нет'}")


if __name__ == "__main__":
    main()
