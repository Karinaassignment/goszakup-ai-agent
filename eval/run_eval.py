"""Оценка точности агента.

Точность фактов — доля эталонных фактов (независимый SQL из eval/cases*.py), верно приведённых в ответе.
Отдельно проверяется, что ID, даты, номера и БИН в Top-K совпадают с базой (нет выдуманных значений),
а также формат ответа, тип вопроса и извлечённые параметры.

Запуск: python -m eval.run_eval [--set main|holdout] [--verbose]
"""
import json
import re
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from agent.core import Agent
from agent.i18n import T
from eval import cases, cases_holdout

TARGET = 85.0
OUT = Path(__file__).resolve().parent
SETS = {
    "main": ("Основной набор", cases.CASES),
    "holdout": ("Отложенная выборка (вопросы заказчика)", cases_holdout.CASES),
}

NUM_RE = re.compile(r"[-+]?\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|[-+]?\d+(?:[.,]\d+)?")
KZ_RE = re.compile("[әғқңөұүһі]", re.I)


def numbers_in(text):
    out = []
    for m in NUM_RE.findall(text):
        try:
            out.append(float(re.sub(r"[   ]", "", m).replace(",", ".")))
        except ValueError:
            pass
    return out


def fact_kind(name):
    if name.endswith(("_id", "_bin", "_number", "_date")):
        return "text"
    if name.startswith("n_"):
        return "int"
    for suffix in ("money", "pct", "qty"):
        if name.endswith("_" + suffix):
            return suffix
    return "price"


TOL = {"int": 0.0, "money": 1.0, "price": 0.01, "pct": 0.1, "qty": 1.0}


def check_fact(name, value, text, nums):
    if value is None:
        return None
    kind = fact_kind(name)
    if kind == "text":
        return str(value) in text
    v = float(value)
    return any(abs(x - v) <= TOL[kind] + 1e-9 for x in nums)


def plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def verify_examples(db, result, text):
    """Сверка выведенных ячеек Top-K (ID, №, дата, БИН) с core.contract."""
    checked, ok, errors = 0, 0, []
    shown_cols = {key for key, _ in result.columns}
    for row in result.examples:
        cid = row.get("contract_id")
        if not cid:
            continue
        truth = db.one("""SELECT id, contract_number, to_char(sign_date, 'DD.MM.YYYY') AS sign_date, customer_bin
                          FROM core.contract WHERE id = %(id)s""", {"id": cid})
        if not truth:
            checked += 1
            errors.append(f"договор {cid} не существует")
            continue
        for key in ("contract_id", "contract_number", "sign_date", "customer_bin"):
            if key not in shown_cols or row.get(key) is None:
                continue
            shown = row[key].strftime("%d.%m.%Y") if isinstance(row[key], (date, datetime)) else str(row[key])
            expected = str(truth["id"] if key == "contract_id" else truth[key])
            checked += 1
            if shown == expected and (shown[:57] in text):
                ok += 1
            else:
                errors.append(f"{key} договора {cid}: в ответе «{shown}», в базе «{expected}»")
    return checked, ok, errors


def verify_bins(db, text):
    bins = set(re.findall(r"(?<!\d)\d{12}(?!\d)", text))
    if not bins:
        return 0, 0, []
    known = {r["bin"] for r in db.query("SELECT bin FROM core.subject WHERE bin = ANY(%(b)s)", {"b": list(bins)})}
    missing = sorted(bins - known)
    return len(bins), len(bins) - len(missing), [f"БИН {b} отсутствует в базе" for b in missing]


def check_format(text, result, lang):
    """6 разделов по порядку, вывод 1–3 предложения, уверенность, Top-K с ID и ссылками."""
    issues = []
    heads = [T[k][lang] for k in ("h_summary", "h_data", "h_comparison", "h_metric", "h_limits")]
    heads.append(T["h_examples"][lang].split(" (")[0])
    pos = [text.find("### " + h) for h in heads]
    if -1 in pos or pos != sorted(pos):
        issues.append("нет всех 6 разделов в нужном порядке")
    summary = " ".join(result.summary)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[А-ЯA-ZӘҒҚҢӨҰҮҺІ])", summary) if s.strip()]
    if not 1 <= len(sentences) <= 3:
        issues.append(f"краткий вывод — {len(sentences)} предложений")
    if f"**{T['confidence'][lang]}:" not in text:
        issues.append("нет уверенности в разделе 5")
    if result.examples:
        keys = {k for k, _ in result.columns}
        if "contract_id" not in keys:
            issues.append("Top-K без ID закупок")
        all_without_anno = all(not row.get("anno_url") for row in result.examples)
        if "anno_url" not in keys:
            issues.append("Top-K без колонки ссылок")
        elif "](https://goszakup.gov.kz/" not in text and not (
                all_without_anno and ("нет объявления на портале" in text or "порталда хабарландыру жоқ" in text)):
            issues.append("Top-K без прямых ссылок")
    return issues


def evaluate_case(agent, case):
    db = agent.db
    started = time.time()
    res = agent.ask(case["q"], with_sql=False)
    p, r, text = res["params"], res["result"], res["text"]
    nums = numbers_in(text)

    truth = {}
    if case.get("truth"):
        truth = {k: plain(v) for k, v in db.one(case["truth"]).items()}
    facts = {k: check_fact(k, v, text, nums) for k, v in truth.items()}
    facts = {k: v for k, v in facts.items() if v is not None}
    for phrase in case.get("expect_text", []):
        facts[f"текст «{phrase}»"] = phrase in text

    ex_n, ex_ok, ex_err = verify_examples(db, r, text)
    bin_n, bin_ok, bin_err = verify_bins(db, text)

    params = {}
    for key, attr in (("bins", "bins"), ("years", "years"), ("enstru", "enstru"), ("lot", "lot_ref")):
        if key in case:
            got = getattr(p, attr)
            params[key] = (sorted(got) == sorted(case[key])) if isinstance(case[key], list) else (got == case[key])
    if "lang" in case:
        params["lang"] = p.lang == case["lang"]

    allowed = case["qtype"] if isinstance(case["qtype"], tuple) else (case["qtype"],)
    fmt_issues = check_format(text, r, p.lang)
    return dict(
        format_ok=not fmt_issues, format_issues=fmt_issues,
        id=case["id"], question=case["q"], expected_type="/".join(allowed), got_type=p.qtype,
        type_ok=p.qtype in allowed,
        facts={k: bool(v) for k, v in facts.items()}, truth=truth,
        facts_ok=sum(facts.values()), facts_n=len(facts),
        examples_ok=ex_ok + bin_ok, examples_n=ex_n + bin_n, errors=ex_err + bin_err,
        params=params, params_ok=sum(params.values()), params_n=len(params),
        seconds=round(time.time() - started, 1), answer=text,
    )


def run_set(agent, key, verbose):
    title, case_list = SETS[key]
    t0 = time.time()
    print(f"\n{title}: {len(case_list)} вопросов")
    print(f"{'ID':<4} {'Тип':<18} {'Факты':>7} {'Top-K':>7} {'Тип?':>5} {'Парам.':>7}  Вопрос")
    print("-" * 110)
    rows = []
    for case in case_list:
        row = evaluate_case(agent, case)
        rows.append(row)
        mark = lambda ok, n: f"{ok}/{n}" if n else "—"
        good = (row["facts_ok"] == row["facts_n"] and row["examples_ok"] == row["examples_n"] and row["type_ok"]
                and row["format_ok"])
        got = row["got_type"] if not row["type_ok"] else row["expected_type"]
        print(f"{row['id']:<4} {got:<18} {mark(row['facts_ok'], row['facts_n']):>7} "
              f"{mark(row['examples_ok'], row['examples_n']):>7} {'да' if row['type_ok'] else 'НЕТ':>5} "
              f"{mark(row['params_ok'], row['params_n']):>7}  {'✅' if good else '❌'} {row['question'][:58]}")
        if verbose and not good:
            missed = [k for k, v in row["facts"].items() if not v]
            if missed:
                print(f"      не найдено: {missed}  эталон: { {k: row['truth'].get(k) for k in missed} }")
            if not row["type_ok"]:
                print(f"      тип: ожидался {row['expected_type']}, получен {row['got_type']}")
            for issue in row["format_issues"]:
                print(f"      формат: {issue}")
            for e in row["errors"]:
                print(f"      ошибка: {e}")
    return rows, summarize(rows, time.time() - t0, title)


def summarize(rows, seconds, title):
    s = lambda key: sum(r[key] for r in rows)
    recall_ok, recall_n = s("facts_ok"), s("facts_n")
    prec_ok, prec_n = s("examples_ok"), s("examples_n")
    by_type = {}
    for r in rows:
        t = by_type.setdefault(r["expected_type"].split("/")[0], [0, 0])
        t[0] += r["facts_ok"]
        t[1] += r["facts_n"]
    acc = recall_ok / recall_n * 100 if recall_n else 0
    pct = lambda ok, n: round(ok / n * 100, 1) if n else None
    return dict(
        title=title, questions=len(rows), seconds=round(seconds, 1),
        fact_accuracy=round(acc, 1), facts_ok=recall_ok, facts_n=recall_n,
        recall=pct(recall_ok, recall_n), recall_ok=recall_ok, recall_n=recall_n,
        precision=pct(prec_ok, prec_n), precision_ok=prec_ok, precision_n=prec_n,
        type_accuracy=pct(sum(r["type_ok"] for r in rows), len(rows)),
        format_ok=sum(r["format_ok"] for r in rows),
        params_accuracy=pct(s("params_ok"), s("params_n")),
        fully_correct=sum(1 for r in rows if r["facts_ok"] == r["facts_n"] and r["examples_ok"] == r["examples_n"]
                          and r["type_ok"] and r["format_ok"]),
        by_type={k: pct(*v) for k, v in by_type.items()},
        passed=acc >= TARGET, target=TARGET,
        langs={"ru": sum(1 for r in rows if not KZ_RE.search(r["question"])),
               "kz": sum(1 for r in rows if KZ_RE.search(r["question"]))},
    )


def print_summary(s):
    bar_len = 40
    filled = int(round(s["fact_accuracy"] / 100 * bar_len))
    bar = "█" * filled + "░" * (bar_len - filled)
    status = "✅ ТРЕБОВАНИЕ ТЗ ВЫПОЛНЕНО" if s["passed"] else "❌ ТРЕБОВАНИЕ ТЗ НЕ ВЫПОЛНЕНО"
    print("\n" + "═" * 72)
    print(f"  ИТОГ: {s['title'].upper()}")
    print("═" * 72)
    print(f"  ТОЧНОСТЬ ФАКТОВ (цифры, даты, БИН):  {s['fact_accuracy']:5.1f}%   ({s['facts_ok']}/{s['facts_n']} эталонных фактов)")
    print(f"  {bar}  порог {s['target']:.0f}%")
    print(f"  {status}")
    print("─" * 72)
    print(f"  Вопросов отвечено полностью верно:   {s['fully_correct']}/{s['questions']}")
    print(f"  Нет выдуманных фактов (Top-K, БИН):  {s['precision']}%   ({s['precision_ok']}/{s['precision_n']})")
    print(f"  Формат ответа ТЗ п. 8:               {s['format_ok']}/{s['questions']} ответов")
    print(f"  Определение типа вопроса:            {s['type_accuracy']}%")
    print(f"  Извлечение параметров:               {s['params_accuracy']}%")
    print("  По типам вопросов:  " + ", ".join(f"{k} {v}%" for k, v in s["by_type"].items()))
    print(f"  Вопросов: {s['questions']} (RU {s['langs']['ru']}, KZ {s['langs']['kz']}), время: {s['seconds']} с")
    print("═" * 72)


def report_section(rows, s):
    lines = [
        f"## {s['title']}",
        "",
        f"**Точность фактов: {s['fact_accuracy']}%** ({s['facts_ok']}/{s['facts_n']}) — "
        f"{'требование ТЗ (≥ 85%) выполнено' if s['passed'] else 'требование ТЗ (≥ 85%) НЕ выполнено'}. "
        f"Вопросов: {s['questions']} (RU {s['langs']['ru']}, KZ {s['langs']['kz']}), полностью верно — "
        f"{s['fully_correct']}.",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        f"| Точность фактов: эталонные факты верно приведены в ответе | {s['recall']}% ({s['recall_ok']}/{s['recall_n']}) |",
        f"| Вопросов отвечено полностью верно | {s['fully_correct']}/{s['questions']} |",
        f"| Нет выдуманных фактов: Top-K и БИН совпадают с базой | {s['precision']}% ({s['precision_ok']}/{s['precision_n']}) |",
        f"| Формат ответа ТЗ п. 8 (6 разделов, вывод 1–3 предложения, Top-K с ID и ссылками) | {s['format_ok']}/{s['questions']} |",
        f"| Определение типа вопроса | {s['type_accuracy']}% |",
        f"| Извлечение параметров | {s['params_accuracy']}% |",
        "",
        "| ID | Тип | Вопрос | Факты | Top-K/БИН | Тип верен | Параметры |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['id']} | {r['expected_type']} | {r['question']} | {r['facts_ok']}/{r['facts_n']} | "
                     f"{r['examples_ok']}/{r['examples_n']} | {'да' if r['type_ok'] else 'нет (' + r['got_type'] + ')'} | "
                     f"{r['params_ok']}/{r['params_n']} |")
    errors = [(r["id"], k, r["truth"].get(k)) for r in rows for k, v in r["facts"].items() if not v]
    errors += [(r["id"], e, "") for r in rows for e in r["errors"]]
    lines += ["", "**Ошибки:**", ""]
    lines += [f"- {i}: {k} {('(эталон: ' + str(v) + ')') if v not in ('', None) else ''}" for i, k, v in errors] or ["Нет."]
    return lines + [""]


def write_report(results):
    lines = [
        "# Оценка точности AI-агента",
        "",
        f"Дата: {datetime.now():%d.%m.%Y %H:%M}.",
        "",
        "**Методика.** Эталон каждого вопроса — отдельный SQL к базовым таблицам (eval/cases*.py), написанный "
        "независимо от инструментов агента. Точность фактов — доля эталонных фактов, верно приведённых в ответе: "
        "ID, БИН, номера и даты — дословно; количества — точно; суммы — ±1 тг; цены — ±0.01; проценты — ±0.1. "
        "Отдельная проверка «нет выдуманных фактов»: каждая ячейка таблицы Top-K (ID договора, №, дата, БИН заказчика) "
        "сверяется с core.contract, каждый БИН в тексте — с core.subject. Отложенная выборка — вопросы, которые не "
        "использовались при разработке агента; её результат до доработки агента сохранён в истории ниже.",
        "",
    ]
    for rows, s in results:
        lines += report_section(rows, s)
    baseline = OUT / "baseline_results.json"
    if baseline.exists():
        lines += ["## История: отложенная выборка до доработки агента", "",
                  "Первый прогон вопросов, которые агент не видел, — честная оценка обобщения на новые формулировки. "
                  "После него агент дорабатывался по найденным ошибкам, поэтому текущий результат на этих вопросах уже "
                  "не является независимым; для новой независимой оценки нужен новый набор вопросов.", ""]
        for item in json.loads(baseline.read_text(encoding="utf-8")):
            b = item["summary"]
            lines.append(f"- {b['title']}: точность фактов {b['fact_accuracy']}% ({b['facts_ok']}/{b['facts_n']}), "
                         f"полностью верно {b['fully_correct']}/{b['questions']}, тип вопроса {b['type_accuracy']}%")
        lines.append("")
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT / "results.json").write_text(json.dumps(
        [{"summary": s, "cases": [{k: v for k, v in r.items() if k != "answer"} for r in rows]} for rows, s in results],
        ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    verbose = "--verbose" in sys.argv
    chosen = sys.argv[sys.argv.index("--set") + 1] if "--set" in sys.argv else "all"
    keys = list(SETS) if chosen == "all" else [chosen]
    agent = Agent()
    results = [run_set(agent, k, verbose) for k in keys]
    for _, s in results:
        print_summary(s)
    write_report(results)
    print(f"  Подробный отчёт: {OUT / 'report.md'}")


if __name__ == "__main__":
    main()
