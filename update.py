"""Инкрементальное обновление: новые и изменённые записи с последнего indexDate → raw/incremental/ → load.py.

Запуск: python update.py   (раз в сутки, см. scheduler/)
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2

import ingest
import market
from load import DSN, DATA

ROOT = Path(__file__).resolve().parent
# курсор включительный, минуты хватает; запас в часы тянул каждый день массовые переиндексации старых лотов
OVERLAP = timedelta(minutes=1)
DEFAULT_LOOKBACK = timedelta(days=30)
MIN_YEAR = 2024
LIMIT = 200


def load_watermarks():
    with psycopg2.connect(**DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT entity, scope, last_index_date FROM etl.watermark")
        wm = {(e, s): d for e, s, d in cur.fetchall()}
        cur.execute("SELECT max(index_date) FROM raw.contract WHERE NOT is_target")
        wm[("Market", "all")] = cur.fetchone()[0]
    return wm


def since(wm, entity, scope):
    ts = wm.get((entity, scope))
    if ts is None:
        known = [v for (e, _), v in wm.items() if e == entity and v]
        ts = min(known) if known else datetime.now() - DEFAULT_LOOKBACK
    return (ts - OVERLAP).strftime("%Y-%m-%d %H:%M:%S")


def fetch_after_index_date(entity, args, fields, cursor):
    # курсор after_index_date включительный, поэтому записи на границе страниц дедуплицируем по id
    rows_by_id = {}
    while True:
        d = ingest.post('{ %s(%s after_index_date: "%s", limit: %d) { %s } }' % (entity, args, cursor, LIMIT, fields))
        rows = (d.get("data") or {}).get(entity) or []
        page = (d.get("extensions") or {}).get("pageInfo") or {}
        new = sum(1 for r in rows if r["id"] not in rows_by_id)
        rows_by_id.update({r["id"]: r for r in rows})
        nxt = (page.get("lastIndexDate") or "").replace("T", " ")[:19]
        if not rows or not page.get("hasNextPage") or not new or not nxt:
            break
        if nxt == cursor and len(rows) == LIMIT:
            print(f"  ! {entity}: больше {LIMIT} записей с одним indexDate {cursor} — часть может догрузиться завтра")
            break
        cursor = nxt
        time.sleep(ingest.PAUSE)
    return list(rows_by_id.values())


def fetch_lots(bin_, cursor):
    rows, after = [], None
    while True:
        args = 'filter: {customerBin: "%s", indexDate: "%s"}, limit: %d' % (bin_, cursor, LIMIT)
        if after:
            args += ", after: %d" % after
        d = ingest.post("{ Lots(%s) { %s } }" % (args, ingest.LOTS_FIELDS))
        page_rows = (d.get("data") or {}).get("Lots") or []
        page = (d.get("extensions") or {}).get("pageInfo") or {}
        rows += page_rows
        after = page.get("lastId")
        if not page_rows or not page.get("hasNextPage") or after is None:
            break
        time.sleep(ingest.PAUSE)
    return rows


def lot_in_window(lot):
    tb = lot.get("TrdBuy") or {}
    years = tb.get("finYear") or []
    years = years if isinstance(years, list) else [years]
    return (tb.get("publishDate") or "") >= f"{MIN_YEAR}-01-01" or any((y or 0) >= MIN_YEAR for y in years)


def save(out_dir, name, rows):
    if rows:
        with open(out_dir / f"{name}.jsonl", "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return len(rows)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    started = datetime.now()
    out_dir = DATA / "raw" / "incremental" / started.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    wm = load_watermarks()
    totals = {"Contract": 0, "Plans": 0, "Lots": 0, "Market": 0}
    failed = []

    print(f"Инкрементальное обновление {started:%d.%m.%Y %H:%M}, запас {OVERLAP}", flush=True)
    for i, b in enumerate(ingest.BINS, 1):
        try:
            rows = fetch_after_index_date("Contract", 'filter: {customerBin: "%s"},' % b, ingest.CONTRACT_FIELDS,
                                          since(wm, "Contract", b))
            totals["Contract"] += save(out_dir, f"Contract_{b}", [r for r in rows if (r.get("finYear") or 0) >= MIN_YEAR])
            rows = fetch_after_index_date("Plans", 'filter: {subjectBiin: "%s"},' % b, ingest.PLANS_FIELDS,
                                          since(wm, "Plans", b))
            totals["Plans"] += save(out_dir, f"Plans_{b}", [r for r in rows if (r.get("plnPointYear") or 0) >= MIN_YEAR])
            totals["Lots"] += save(out_dir, f"Lots_{b}", [r for r in fetch_lots(b, since(wm, "Lots", b)) if lot_in_window(r)])
        except RuntimeError as e:
            print(f"  ОШИБКА {b}: {e}")
            failed.append(b)
        print(f"[{i}/{len(ingest.BINS)}] {b}: договоры {totals['Contract']}, планы {totals['Plans']}, лоты {totals['Lots']}")

    codes_file = market.CODES_FILE
    codes = json.loads(codes_file.read_text(encoding="utf-8")) if codes_file.exists() else []
    cursor = since(wm, "Market", "all")
    for code in codes:
        try:
            rows = fetch_after_index_date("Contract", 'contractUnitsFilter: {refEnstruCode: "%s"},' % code,
                                          market.FIELDS, cursor)
            totals["Market"] += save(out_dir, f"Market_{code}",
                                     [r for r in rows if (r.get("finYear") or 0) >= MIN_YEAR])
        except RuntimeError as e:
            print(f"  ОШИБКА рынок {code}: {e}")
            failed.append(code)
    print(f"Рынок: {len(codes)} кодов, договоров {totals['Market']}")

    print(f"\nСкачано за {(datetime.now() - started).seconds} с: {totals}. Ошибки: {failed or 'нет'}")
    if not any(totals.values()):
        print("Новых данных нет — пересборка не нужна.")
        out_dir.rmdir()
        return 0
    print("Загрузка в базу (load.py)...", flush=True)
    return subprocess.call([sys.executable, str(ROOT / "load.py")], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
