import glob, json
from collections import defaultdict

def flat(x):
    return x if isinstance(x, list) else [x]

sums = defaultdict(float)
for path in glob.glob("raw/Contract_*.jsonl"):
    with open(path, encoding="utf-8") as f:
        for line in f:
            for u in json.loads(line).get("ContractUnits") or []:
                for code in flat(u.get("refEnstruCode")):
                    if code:
                        sums[code] += u.get("totalSum") or 0

ordered = sorted(sums.values(), reverse=True)
total = sum(ordered)
print("Всего кодов:", len(ordered))
for n in (50, 100, 200, 300, 500, 800):
    print(f"Топ-{n}: {sum(ordered[:n]) / total * 100:.1f}% суммы")