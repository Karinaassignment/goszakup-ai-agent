import os, glob, json, time, requests
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / "secret_key.env")
DATA = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent)
CODES_FILE = DATA / "market_codes.json"
TOKEN = (os.getenv("OWS_TOKEN") or "").strip().strip('"').strip("'")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
URL = "https://ows.goszakup.gov.kz/v3/graphql"

TOP_CODES = 100
MIN_UNITS = 3
YEARS = [2024, 2025, 2026]
LIMIT = 100
MAX_PAGES = 10  # не больше 1000 договоров на код и год
PAUSE = 0.2
OUT = str(DATA / "raw_market")
os.makedirs(OUT, exist_ok=True)


def flat(x):
    return x if isinstance(x, list) else [x]


def select_codes():
    """Коды ЕНС ТРУ с наибольшей суммой закупок по уже скачанным договорам."""
    stats = defaultdict(lambda: [0, 0.0])
    files = glob.glob(str(DATA / "raw" / "Contract_*.jsonl"))
    print("Файлов с договорами:", len(files))
    for path in files:
        with open(path, encoding="utf-8") as f:
            for line in f:
                c = json.loads(line)
                for u in c.get("ContractUnits") or []:
                    for code in flat(u.get("refEnstruCode")):
                        if code:
                            stats[code][0] += 1
                            stats[code][1] += u.get("totalSum") or 0

    codes = [c for c, (n, s) in stats.items() if n >= MIN_UNITS]
    codes.sort(key=lambda c: stats[c][1], reverse=True)
    codes = codes[:TOP_CODES]
    print("Кодов выбрано:", len(codes))
    with open(CODES_FILE, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False, indent=2)
    return codes


FIELDS = """id contractNumber trdBuyId signDate finYear customerBin supplierBiin refContractStatusId withNds
  refCurrencyCode deleted indexDate refSubjectTypeId Supplier { bin iin nameRu }
  ContractUnits { id itemPrice itemPriceWnds quantity totalSum refEnstruCode deleted
    Plans { refUnitsCode PlansKato { refKatoCode } } }"""


def post(query):
    for attempt in range(6):
        try:
            r = requests.post(URL, json={"query": query}, headers=HEADERS, timeout=180)
            if r.status_code == 200:
                d = r.json()
                if d.get("errors") and not d.get("data"):
                    raise RuntimeError(d["errors"][0]["message"])
                return d
            print("HTTP", r.status_code, "- повтор через", 5 * (attempt + 1), "сек")
        except requests.RequestException as e:
            print("Сеть:", e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("Нет ответа после 6 попыток")


def write_text(path, text):
    with open(path, "w") as f:
        f.write(text)


def run_job(code, year):
    base = os.path.join(OUT, f"{code}_{year}")
    data_file, state_file, done_file = base + ".jsonl", base + ".state", base + ".done"
    if os.path.exists(done_file):
        return 0
    after = int(open(state_file).read()) if os.path.exists(state_file) else None
    total, pages = 0, 0
    with open(data_file, "a", encoding="utf-8") as f:
        while pages < MAX_PAGES:
            args = 'filter: {finYear: %d}, contractUnitsFilter: {refEnstruCode: "%s"}, limit: %d' % (year, code, LIMIT)
            if after:
                args += ", after: %d" % after
            d = post("{ Contract(%s) { %s } }" % (args, FIELDS))
            rows = (d.get("data") or {}).get("Contract") or []
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            total += len(rows)
            pages += 1
            page = (d.get("extensions") or {}).get("pageInfo") or {}
            after = page.get("lastId")
            if after is not None:
                write_text(state_file, str(after))
            if not rows or not page.get("hasNextPage"):
                break
            time.sleep(PAUSE)
    write_text(done_file, "ok")
    return total


def main():
    codes = select_codes()
    failed = []
    for i, code in enumerate(codes, 1):
        n = 0
        for y in YEARS:
            try:
                n += run_job(code, y)
            except RuntimeError as e:
                print("ОШИБКА:", code, y, "->", e)
                failed.append((code, y))
        print(f"[{i}/{len(codes)}] {code}: +{n} договоров")

    print("Готово. Ошибки:", failed if failed else "нет")


if __name__ == "__main__":
    main()