# import os, time, json, requests
# from dotenv import load_dotenv

# load_dotenv("secret_key.env")
# token = (os.getenv("OWS_TOKEN") or "").strip().strip('"').strip("'")
# headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
# url = "https://ows.goszakup.gov.kz/v3/graphql"

# bins = [
#     '000740001307', '020240002363', '020440003656', '030440003698',
#     '050740004819', '051040005150', '100140011059', '120940001946', '140340016539',
#     '150540000186', '171041003124', '210240019348', '210240033968', '210941010761',
#     '230740013340', '231040023028', '780140000023', '900640000128', '940740000911',
#     '940940000384', '960440000220', '970940001378', '971040001050', '980440001034',
#     '981140001551', '990340005977', '990740002243',
# ]

# def gql(query):
#     r = requests.post(url, json={"query": query}, headers=headers, timeout=60)
#     return r.status_code, r.json()

# # Часть 1: найти все БИНы
# found = {}
# for b in bins:
#     query = '{ Subjects(filter: {bin: "%s"}) { pid nameRu bin } }' % b
#     code, data = gql(query)
#     items = (data.get("data") or {}).get("Subjects") or []
#     found[b] = items[0] if items else None
#     print(b, "->", items[0]["nameRu"][:60] if items else "НЕ НАЙДЕН")
#     time.sleep(0.5)

# with open("subjects.json", "w", encoding="utf-8") as f:
#     json.dump(found, f, ensure_ascii=False, indent=2)

# # Часть 2: список сущностей (интроспекция)
# code, data = gql("{ __schema { queryType { fields { name } } } }")
# print("Интроспекция:", code)
# if code == 200 and "data" in data and data["data"]:
#     names = [x["name"] for x in data["data"]["__schema"]["queryType"]["fields"]]
#     with open("entities.txt", "w", encoding="utf-8") as f:
#         f.write("\n".join(names))
#     print("Найдено сущностей:", len(names))
# else:
#     print(json.dumps(data, ensure_ascii=False)[:500]) 


# import os, requests
# from dotenv import load_dotenv

# load_dotenv("secret_key.env")
# token = (os.getenv("OWS_TOKEN") or "").strip().strip('"').strip("'")
# headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
# url = "https://ows.goszakup.gov.kz/v3/graphql"

# def gql(query):
#     return requests.post(url, json={"query": query}, headers=headers, timeout=60).json()

# TYPE = "kind name ofType { kind name ofType { kind name ofType { kind name } } }"

# def unwrap(t):
#     while t and t.get("name") is None and t.get("ofType"):
#         t = t["ofType"]
#     return t.get("name") if t else None

# wanted = ["TrdBuy", "ObTrdBuy", "Lots", "ObLots", "Contract", "ObContract",
#           "Plans", "ObPlans", "TrdApp"]

# q = "{ __schema { queryType { fields { name type { %s } args { name } } } } }" % TYPE
# fields = {f["name"]: f for f in gql(q)["data"]["__schema"]["queryType"]["fields"]}

# lines = []
# for name in wanted:
#     f = fields[name]
#     tname = unwrap(f["type"])
#     lines.append(f"=== {name}  (тип: {tname})")
#     lines.append("   аргументы: " + ", ".join(a["name"] for a in f["args"]))
#     d = gql('{ __type(name: "%s") { fields { name type { %s } } } }' % (tname, TYPE))
#     for x in d["data"]["__type"]["fields"]:
#         lines.append(f"   - {x['name']}: {unwrap(x['type'])}")
#     lines.append("")

# with open("schema.txt", "w", encoding="utf-8") as fh:
#     fh.write("\n".join(lines))
# print("Готово") 


# import os, csv, time, requests
# from dotenv import load_dotenv

# load_dotenv("secret_key.env")
# token = (os.getenv("OWS_TOKEN") or "").strip().strip('"').strip("'")
# headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
# url = "https://ows.goszakup.gov.kz/v3/graphql"

# def gql(query):
#     return requests.post(url, json={"query": query}, headers=headers, timeout=60).json()

# bins = [
#     '000740001307', '020240002363', '020440003656', '030440003698',
#     '050740004819', '051040005150', '100140011059', '120940001946', '140340016539',
#     '150540000186', '171041003124', '210240019348', '210240033968', '210941010761',
#     '230740013340', '231040023028', '780140000023', '900640000128', '940740000911',
#     '940940000384', '960440000220', '970940001378', '971040001050', '980440001034',
#     '981140001551', '990340005977', '990740002243',
# ]

# # (сущность, поле БИН для фильтра)
# checks = [
#     ("TrdBuy", "customerBin"), ("ObTrdBuy", "customerBin"),
#     ("Lots", "customerBin"), ("ObLots", "customerBin"),
#     ("Contract", "customerBin"), ("ObContract", "customerBin"),
#     ("Plans", "subjectBiin"), ("ObPlans", "subjectBiin"),
# ]

# rows = []
# for b in bins:
#     row = {"bin": b}
#     for ent, field in checks:
#         q = '{ %s(filter: {%s: "%s"}, limit: 1) { id } }' % (ent, field, b)
#         d = gql(q)
#         if d.get("errors"):
#             row[ent] = "ERR: " + d["errors"][0]["message"][:60]
#         else:
#             row[ent] = d["extensions"]["pageInfo"]["totalCount"]
#         time.sleep(0.3)
#     print(row)
#     rows.append(row)

# with open("counts.csv", "w", newline="", encoding="utf-8") as f:
#     w = csv.DictWriter(f, fieldnames=["bin"] + [c[0] for c in checks])
#     w.writeheader()
#     w.writerows(rows)

# # Поля вложенных сущностей, которых не хватало
# TYPE = "kind name ofType { kind name ofType { kind name } }"
# def unwrap(t):
#     while t and t.get("name") is None and t.get("ofType"):
#         t = t["ofType"]
#     return t.get("name") if t else None

# out = []
# for tname in ["ContractUnits", "ObContractUnits", "TrdAppLots", "PlnPointsKato",
#             "RefEnstru", "RefUnits", "Subject", "RefLotsStatus",
#             "RefBuyStatus", "RefContractStatus", "RefTradeMethods"]:
#     d = gql('{ __type(name: "%s") { fields { name type { %s } } } }' % (tname, TYPE))
#     t = (d.get("data") or {}).get("__type")
#     out.append(f"=== {tname}")
#     for x in (t or {}).get("fields") or []:
#         out.append(f"   - {x['name']}: {unwrap(x['type'])}")
#     out.append("")
# with open("schema2.txt", "w", encoding="utf-8") as f:
#     f.write("\n".join(out))
# print("Готово") 

import os, json, requests
from dotenv import load_dotenv

load_dotenv("secret_key.env")
token = (os.getenv("OWS_TOKEN") or "").strip().strip('"').strip("'")
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
url = "https://ows.goszakup.gov.kz/v3/graphql"

def gql(query):
    return requests.post(url, json={"query": query}, headers=headers, timeout=120).json()

TYPE = "kind name ofType { kind name ofType { kind name ofType { kind name } } }"
def unwrap(t):
    while t and t.get("name") is None and t.get("ofType"):
        t = t["ofType"]
    return t.get("name") if t else None

# ---------- Часть 1: какие поля принимают фильтры ----------
q = "{ __schema { queryType { fields { name args { name type { %s } } } } } }" % TYPE
fields = {f["name"]: f for f in gql(q)["data"]["__schema"]["queryType"]["fields"]}

lines = []
for ent in ["TrdBuy", "Lots", "Contract", "ObContract", "Plans", "TrdApp"]:
    for a in fields[ent]["args"]:
        if a["name"] in ("filter", "contractUnitsFilter"):
            tname = unwrap(a["type"])
            d = gql('{ __type(name: "%s") { inputFields { name type { %s } } } }' % (tname, TYPE))
            lines.append(f"=== {ent}.{a['name']}  ({tname})")
            for x in (d.get("data") or {}).get("__type", {}).get("inputFields") or []:
                lines.append(f"   - {x['name']}: {unwrap(x['type'])}")
            lines.append("")
with open("filters.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("filters.txt готов")

# ---------- Часть 2: образцы данных по одной организации ----------
BIN = "000740001307"

contract_q = """
{
  Contract(filter: {customerBin: "%s"}, limit: 5) {
    id contractNumber signDate finYear contractSum refCurrencyCode
    customerBin supplierBiin trdBuyId refContractStatusId
    RefContractStatus { nameRu }
    ContractUnits {
      id lotId plnPointId itemPrice quantity totalSum refEnstruCode
      Plans {
        nameRu refUnitsCode refEnstruCode price count
        RefUnits { nameRu code }
        RefEnstru { code nameRu edizm }
        PlansKato { refKatoCode fullDeliveryPlaceNameRu }
      }
    }
  }
}
""" % BIN

lots_q = """
{
  Lots(filter: {customerBin: "%s"}, limit: 5) {
    id lotNumber nameRu count amount trdBuyId refLotStatusId enstruList plnPointKatoList
    RefLotsStatus { nameRu }
    TrdBuy { id numberAnno publishDate finYear kato refTradeMethodsId RefTradeMethods { nameRu } }
  }
}
""" % BIN

for name, query in [("sample_contract.json", contract_q), ("sample_lots.json", lots_q)]:
    d = gql(query)
    with open(name, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(name, "готов", "(есть ошибки)" if d.get("errors") else "")