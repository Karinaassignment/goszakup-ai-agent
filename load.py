import os, glob, json, time, argparse
from datetime import datetime, timezone
from pathlib import Path
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR") or ROOT)
load_dotenv(ROOT / "db.env")
DSN = dict(
    host=os.getenv("PG_HOST", "localhost"),
    port=int(os.getenv("PG_PORT", "5433")),
    dbname=os.getenv("POSTGRES_DB", "goszakup"),
    user=os.getenv("POSTGRES_USER", "etl"),
    password=os.getenv("POSTGRES_PASSWORD"),
)
BATCH = 2000

# (шаблон файлов, таблица raw, is_target для договоров)
SOURCES = [
    ("raw/Contract_*.jsonl", "contract", True),
    ("raw_market/*.jsonl", "contract", False),
    ("raw/Plans_*.jsonl", "plan_point", None),
    ("raw/Lots_*.jsonl", "lot", None),
    ("raw/incremental/*/Contract_*.jsonl", "contract", True),
    ("raw/incremental/*/Market_*.jsonl", "contract", False),
    ("raw/incremental/*/Plans_*.jsonl", "plan_point", None),
    ("raw/incremental/*/Lots_*.jsonl", "lot", None),
    # патчи enrich.py дописывают поля в уже загруженные записи, поэтому идут последними
    ("raw/enrich/Contract_*.jsonl", "contract_patch", None),
    ("raw/enrich/Plans_*.jsonl", "plan_point_patch", None),
]

PATCH = """
UPDATE raw.{table} AS t SET payload = t.payload || v.patch::jsonb
FROM (VALUES %s) AS v(id, patch)
WHERE t.id = v.id"""

# обновляем только более свежей версией; версия из выгрузки заказчиков важнее рыночной (в ней больше полей)
UPSERT_CONTRACT = """
INSERT INTO raw.contract AS t (id, payload, is_target, index_date, source_file) VALUES %s
ON CONFLICT (id) DO UPDATE
SET payload = EXCLUDED.payload, is_target = t.is_target OR EXCLUDED.is_target,
    index_date = EXCLUDED.index_date, source_file = EXCLUDED.source_file, loaded_at = now()
WHERE (EXCLUDED.is_target AND NOT t.is_target)
   OR (EXCLUDED.is_target = t.is_target AND coalesce(EXCLUDED.index_date >= t.index_date, true))"""

UPSERT_PLAIN = """
INSERT INTO raw.{table} AS t (id, payload, index_date, source_file) VALUES %s
ON CONFLICT (id) DO UPDATE
SET payload = EXCLUDED.payload, index_date = EXCLUDED.index_date,
    source_file = EXCLUDED.source_file, loaded_at = now()
WHERE coalesce(EXCLUDED.index_date >= t.index_date, true)"""


def read_jsonl(path):
    """Строки файла без дублей по id: после перезапуска ingest.py страница могла записаться дважды."""
    rows, n = {}, 0
    with open(DATA / path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n += 1
            line = line.replace("\\u0000", "")      # Postgres не принимает \u0000 в jsonb
            rec = json.loads(line)
            old = rows.get(rec["id"])
            if old is None or (rec.get("indexDate") or "") >= (old[0].get("indexDate") or ""):
                rows[rec["id"]] = (rec, line)
    return n, list(rows.values())


def file_unchanged(cur, path, mtime):
    cur.execute("SELECT file_mtime FROM etl.load_file WHERE file_path = %s", (path,))
    r = cur.fetchone()
    return r is not None and r[0] == mtime


def load_file(cur, run_id, path, table, is_target):
    n_read, rows = read_jsonl(path)
    if table.endswith("_patch"):
        values = [(r["id"], line) for r, line in rows]
        upserted = 0
        for i in range(0, len(values), BATCH):
            execute_values(cur, PATCH.format(table=table[:-len("_patch")]), values[i:i + BATCH],
                           template="(%s::bigint, %s)", page_size=BATCH)
            upserted += cur.rowcount
        return n_read, upserted
    if table == "contract":
        sql, tpl = UPSERT_CONTRACT, "(%s, %s::jsonb, %s, %s::timestamp, %s)"
        values = [(r["id"], line, is_target, r.get("indexDate"), path) for r, line in rows]
    else:
        sql, tpl = UPSERT_PLAIN.format(table=table), "(%s, %s::jsonb, %s::timestamp, %s)"
        values = [(r["id"], line, r.get("indexDate"), path) for r, line in rows]
    upserted = 0
    for i in range(0, len(values), BATCH):
        execute_values(cur, sql, values[i:i + BATCH], template=tpl, page_size=BATCH)
        upserted += cur.rowcount
    return n_read, upserted


def load_subjects(cur):
    with open(ROOT / "subjects.json", encoding="utf-8") as f:
        subjects = json.load(f)
    values = [(b, json.dumps(s, ensure_ascii=False), "subjects.json") for b, s in subjects.items()]
    execute_values(cur, """
        INSERT INTO raw.subject (bin, payload, source_file) VALUES %s
        ON CONFLICT (bin) DO UPDATE SET payload = EXCLUDED.payload, loaded_at = now()""",
        values, template="(%s, %s::jsonb, %s)")
    return len(values)


def main():
    ap = argparse.ArgumentParser(description="JSONL -> raw -> ref/core -> mart")
    ap.add_argument("--force", action="store_true", help="перечитать все файлы, даже не изменившиеся")
    args = ap.parse_args()

    conn = psycopg2.connect(**DSN)
    cur = conn.cursor()
    # роль создаётся init-скриптом один раз, поэтому пароль синхронизируем с окружением при каждой загрузке
    if os.getenv("AGENT_DB_PASSWORD"):
        cur.execute(sql.SQL("ALTER ROLE agent_ro PASSWORD {}").format(sql.Literal(os.getenv("AGENT_DB_PASSWORD"))))
        conn.commit()
    cur.execute("INSERT INTO etl.load_run DEFAULT VALUES RETURNING run_id")
    run_id = cur.fetchone()[0]
    conn.commit()
    print("Запуск", run_id)

    try:
        print("Субъекты:", load_subjects(cur))
        conn.commit()

        for pattern, table, is_target in SOURCES:
            files = sorted(glob.glob(pattern, root_dir=DATA))
            skipped = 0
            for path in files:
                path = path.replace("\\", "/")
                mtime = datetime.fromtimestamp(os.path.getmtime(DATA / path), tz=timezone.utc)
                if not args.force and file_unchanged(cur, path, mtime):
                    skipped += 1
                    continue
                n_read, upserted = load_file(cur, run_id, path, table, is_target)
                cur.execute("""
                    INSERT INTO etl.load_file (file_path, entity, file_mtime, rows_read, rows_upserted, run_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (file_path) DO UPDATE
                    SET file_mtime = EXCLUDED.file_mtime, rows_read = EXCLUDED.rows_read,
                        rows_upserted = EXCLUDED.rows_upserted, run_id = EXCLUDED.run_id, loaded_at = now()""",
                    (path, table, mtime, n_read, upserted, run_id))
                conn.commit()
                print(f"  {path}: прочитано {n_read}, записано {upserted}")
            print(f"{pattern}: файлов {len(files)}, пропущено без изменений {skipped}")

        t = time.time()
        print("Пересборка ref/core + проверки качества...")
        cur.execute("SELECT set_config('etl.run_id', %s, false)", (str(run_id),))
        with open(ROOT / "db" / "transform.sql", encoding="utf-8") as f:
            cur.execute(f.read())
        conn.commit()
        print(f"  готово за {time.time() - t:.0f} с")

        t = time.time()
        print("Обновление витрин mart...")
        cur.execute("SELECT mart.refresh_all()")
        conn.commit()
        # без свежей статистики планировщик строит медленные планы для запросов агента
        conn.autocommit = True
        cur.execute("ANALYZE core.contract, core.contract_unit, core.plan_point, core.lot, mart.unit_price, "
                    "mart.enstru_price_stats, mart.region_coef, mart.qty_by_year, mart.qty_anomaly, "
                    "mart.season_coef, mart.fair_price")
        conn.autocommit = False
        print(f"  готово за {time.time() - t:.0f} с")

        import ml_anomaly
        ml_anomaly.run(conn)

        try:
            import rag_index
            rag_index.run(conn)
        except ImportError as e:
            print("RAG: пропущено — не установлен sentence-transformers:", e)

        print("\nСтрок в таблицах:")
        for table in ["raw.contract", "raw.plan_point", "raw.lot", "core.subject", "core.trd_buy",
                      "core.lot", "core.plan_point", "core.contract", "core.contract_unit",
                      "mart.unit_price", "mart.enstru_price_stats", "mart.region_coef", "mart.qty_by_year"]:
            cur.execute(f"SELECT count(*) FROM {table}")
            print(f"  {table:<26} {cur.fetchone()[0]:>10}")

        print("\nПроблемы качества данных:")
        cur.execute("SELECT entity, rule, count(*) FROM etl.dq_issue GROUP BY 1, 2 ORDER BY 1, 3 DESC")
        for entity, rule, n in cur.fetchall():
            print(f"  {entity:<14} {rule:<26} {n:>8}")

        cur.execute("UPDATE etl.load_run SET status = 'ok', finished_at = now() WHERE run_id = %s", (run_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.execute("UPDATE etl.load_run SET status = 'failed', finished_at = now(), note = %s WHERE run_id = %s",
                    (str(e)[:1000], run_id))
        conn.commit()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
