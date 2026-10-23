import os, time, json, requests
from collections import Counter
from dotenv import load_dotenv

load_dotenv("secret_key.env")
token = (os.getenv("OWS_TOKEN") or "").strip().strip('"').strip("'")
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
URL = "https://ows.goszakup.gov.kz/v3/graphql"

MY_BINS = {'000740001307', '020240002363', '020440003656', '030440003698',
    '050740004819', '051040005150', '100140011059', '120940001946', '140340016539',
    '150540000186', '171041003124', '210240019348', '210240033968', '210941010761',
    '230740013340', '231040023028', '780140000023', '900640000128', '940740000911',
    '940940000384', '960440000220', '970940001378', '971040001050', '980440001034',
    '981140001551', '990340005977', '990740002243'}

def flat(x):
    return x if isinstance(x, list) else [x]

def check(code, year=2026, limit=100):
    query = """
    { Contract(filter: {finYear: %d}, contractUnitsFilter: {refEnstruCode: "%s"}, limit: %d) {
        id customerBin signDate
        ContractUnits { refEnstruCode itemPrice quantity
          Plans { RefUnits { code } PlansKato { refKatoCode } } }
    } }
    """ % (year, code, limit)
    t = time.time()
    d = requests.post(URL, json={"query": query}, headers=headers, timeout=180).json()
    sec = time.time() - t
    if d.get("errors") and not d.get("data"):
        print(code, "ОШИБКА:", d["errors"][0]["message"])
        return
    rows = d["data"]["Contract"]
    page = d["extensions"]["pageInfo"]
    with_code = 0
    prices = []
    for c in rows:
        units = [u for u in (c.get("ContractUnits") or [])
                 if code in flat(u.get("refEnstruCode"))]
        if units:
            with_code += 1
            prices += [u["itemPrice"] for u in units if u.get("itemPrice")]
    cust = Counter(c["customerBin"] for c in rows)
    print("-----", code)
    print("Запрос занял, сек:", round(sec, 1), "| договоров в ответе:", len(rows))
    print("totalCount по рынку за год:", page["totalCount"], "| есть следующая страница:", page["hasNextPage"])
    print("Из них с позицией этого кода:", with_code)
    print("Разных заказчиков:", len(cust), "| среди них из ваших 27:", sum(1 for b in cust if b in MY_BINS))
    if prices:
        prices.sort()
        print("Цена за единицу: мин", prices[0], "| медиана", prices[len(prices)//2], "| макс", prices[-1])

check("263013.000.000000")   # Веб-камера
check("262021.900.000098")   # Флеш-накопитель