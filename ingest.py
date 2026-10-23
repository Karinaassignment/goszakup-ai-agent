import os, json, time, requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / "secret_key.env")
DATA = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent)
TOKEN = (os.getenv("OWS_TOKEN") or "").strip().strip('"').strip("'")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
URL = "https://ows.goszakup.gov.kz/v3/graphql"

TEST_ONLY_FIRST_BIN = False
LIMIT = 50
PAUSE = 0.3
YEARS = [2024, 2025, 2026]
INCLUDE_OB = False  # ObContract дублирует Contract без планов
OUT = str(DATA / "raw")
os.makedirs(OUT, exist_ok=True)

BINS = [
    '000740001307', '020240002363', '020440003656', '030440003698',
    '050740004819', '051040005150', '100140011059', '120940001946', '140340016539',
    '150540000186', '171041003124', '210240019348', '210240033968', '210941010761',
    '230740013340', '231040023028', '780140000023', '900640000128', '940740000911',
    '940940000384', '960440000220', '970940001378', '971040001050', '980440001034',
    '981140001551', '990340005977', '990740002243',
]

PLAN_NESTED = """nameRu refUnitsCode refEnstruCode price count
  RefUnits { nameRu code } RefEnstru { code nameRu }
  PlansKato { refKatoCode fullDeliveryPlaceNameRu }"""

# эти же поля enrich.py догружает в уже скачанные записи
CONTRACT_EXTRA = """refSubjectTypeId RefSubjectType { nameRu nameKz } trdBuyNameRu faktSum
  Supplier { bin iin nameRu nameKz }"""
PLANS_EXTRA = "refSubjectTypeId RefSubjectType { nameRu nameKz } RefEnstru { code nameRu nameKz }"

CONTRACT_FIELDS = """id contractNumber signDate finYear contractSum contractSumWnds withNds
  refCurrencyCode exchangeRate customerBin supplierBiin trdBuyId refContractStatusId
  faktTradeMethodsId refContractTypeId deleted lastUpdateDate indexDate
  RefContractStatus { nameRu } %s
  ContractUnits { id lotId plnPointId itemPrice itemPriceWnds quantity totalSum totalSumWnds
    refEnstruCode deleted Plans { %s } }""" % (CONTRACT_EXTRA, PLAN_NESTED)

OBCONTRACT_FIELDS = """id contractNumber signDate finYear contractSum contractSumWnds withNds
  refCurrencyCode exchangeRate customerBin supplierBiin trdBuyId refContractStatusId
  faktTradeMethodsId refContractTypeId deleted lastUpdateDate indexDate
  RefContractStatus { nameRu }
  ObContractUnits { id lotId plnPointId itemPrice itemPriceWnds quantity totalSum totalSumWnds
    deleted ObPlans { %s } }""" % PLAN_NESTED

PLANS_FIELDS = """id rootrecordId subjectBiin nameRu refTradeMethodsId refUnitsCode count price amount
  plnPointYear refEnstruCode refPlnPointStatusId refMonthsId isActive isDeleted indexDate
  RefUnits { nameRu code } RefTradeMethods { nameRu }
  RefPlnPointStatus { nameRu } """ + PLANS_EXTRA + """
  PlansKato { refKatoCode fullDeliveryPlaceNameRu }"""

LOTS_FIELDS = """id lotNumber nameRu count amount trdBuyId refLotStatusId enstruList
  plnPointKatoList customerBin refTradeMethodsId lastUpdateDate indexDate
  RefLotsStatus { nameRu }
  TrdBuy { id numberAnno publishDate finYear kato totalSum countLots refTradeMethodsId
    refBuyStatusId RefTradeMethods { nameRu } RefBuyStatus { nameRu } }"""


def build_query(entity, filt, fields, after):
    args = "filter: {%s}, limit: %d" % (filt, LIMIT)
    if after:
        args += ", after: %d" % after
    return "{ %s(%s) { %s } }" % (entity, args, fields)


def post(query):
    for attempt in range(6):
        try:
            r = requests.post(URL, json={"query": query}, headers=HEADERS, timeout=120)
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


def run_job(name, entity, filt, fields, stop_fn=None):
    base = os.path.join(OUT, name)
    data_file, state_file, done_file = base + ".jsonl", base + ".state", base + ".done"
    if os.path.exists(done_file):
        return
    after = None
    if os.path.exists(state_file):
        after = int(open(state_file).read().strip())
    total = 0
    with open(data_file, "a", encoding="utf-8") as f:
        while True:
            d = post(build_query(entity, filt, fields, after))
            rows = (d.get("data") or {}).get(entity) or []
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            total += len(rows)
            page = (d.get("extensions") or {}).get("pageInfo") or {}
            after = page.get("lastId")
            if after is not None:
                write_text(state_file, str(after))
            if not rows or not page.get("hasNextPage") or (stop_fn and stop_fn(rows)):
                break
            time.sleep(PAUSE)
    write_text(done_file, "ok")
    print(f"{name}: {total} записей")


def lots_too_old(rows):
    dates = [(r.get("TrdBuy") or {}).get("publishDate") for r in rows]
    dates = [x for x in dates if x]
    return bool(dates) and all(x < "2024-01-01" for x in dates)


def main():
    bins = BINS[:1] if TEST_ONLY_FIRST_BIN else BINS
    jobs = []
    for b in bins:
        for y in YEARS:
            jobs.append((f"Contract_{b}_{y}", "Contract", f'customerBin: "{b}", finYear: {y}', CONTRACT_FIELDS, None))
            if INCLUDE_OB:
                jobs.append((f"ObContract_{b}_{y}", "ObContract", f'customerBin: "{b}", finYear: {y}', OBCONTRACT_FIELDS, None))
    for b in bins:
        for y in YEARS:
            jobs.append((f"Plans_{b}_{y}", "Plans", f'subjectBiin: "{b}", plnPointYear: {y}', PLANS_FIELDS, None))
    for b in bins:
        jobs.append((f"Lots_{b}", "Lots", f'customerBin: "{b}"', LOTS_FIELDS, lots_too_old))

    failed = []
    for i, (name, entity, filt, fields, stop_fn) in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {name}")
        try:
            run_job(name, entity, filt, fields, stop_fn)
        except RuntimeError as e:
            print("ОШИБКА:", name, "->", e)
            failed.append(name)

    print("Готово. Ошибки:", failed if failed else "нет")


if __name__ == "__main__":
    main()