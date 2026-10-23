"""Инструменты агента: параметризованные SQL-запросы к витринам mart, результат — Result."""
import os
import re
from dataclasses import dataclass, field

from . import llm, semantic
from .i18n import t, verdict, tier
from .router import CITY_NAMES, _is_stop as is_stop

PORTAL_ANNO = "https://goszakup.gov.kz/ru/announce/index/"

# не действующие сделки (как в mart.unit_price)
BAD_CONTRACT_STATUS = r"(не заключен|^изменен$|на подписании|проект)"


@dataclass
class Result:
    qtype: str
    summary: list = field(default_factory=list)
    data_used: list = field(default_factory=list)
    comparison: list = field(default_factory=list)
    metric: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    confidence: str = "medium"
    confidence_why: str = ""
    n_analyzed: int = None
    n_compared: int = None
    examples: list = field(default_factory=list)       # Top-K закупок с ID и ссылкой
    columns: list = field(default_factory=list)        # [(ключ, заголовок)]
    n_examples_total: int = None
    groups: list = field(default_factory=list)         # таблица сравнения групп
    group_columns: list = field(default_factory=list)
    data_quality: list = field(default_factory=list)


def L(lang, ru, kz):
    return kz if lang == "kz" else ru


def money(x):
    if x is None:
        return "—"
    return f"{x:,.0f}".replace(",", " ") + " тг"


def num(x, d=2):
    if x is None:
        return "—"
    s = f"{x:,.{d}f}".replace(",", " ")
    return s.rstrip("0").rstrip(".") if d else s


def pct(x):
    return "—" if x is None else f"{x:+.1f}%"


def years_txt(p):
    y = p.period
    return f"{y[0]}–{y[-1]}" if len(y) > 1 else str(y[0])


def short_name(subjects, bin_):
    name = subjects.get(bin_) or ""
    # кавычки бывают вложенными — берём самый короткий фрагмент
    parts = [s.strip() for s in re.findall(r"[«\"]([^«»\"]{3,})", name) if len(s.strip()) >= 3]
    if parts:
        return f"{bin_} ({min(parts, key=len)})"
    return f"{bin_} ({name[:40]})" if name else bin_


def _codes_for(db, terms):
    """Коды по словам; сначала те, чьё название начинается с одного из слов («Компьютер», а не «Стол компьютерный»)."""
    pats = [f"%{w}%" for w in terms]
    heads = [f"{w}%" for w in terms]
    rows = db.query("""SELECT code FROM ref.enstru WHERE name_ru ILIKE ALL(%(p)s) AND name_ru ILIKE ANY(%(h)s)
                       ORDER BY code LIMIT 300""", {"p": pats, "h": heads})
    if not rows:
        rows = db.query("SELECT code FROM ref.enstru WHERE name_ru ILIKE ALL(%(p)s) ORDER BY code LIMIT 300", {"p": pats})
    if not rows:
        rows = db.query("""SELECT DISTINCT enstru_code AS code FROM mart.unit_price
                           WHERE name_clean ILIKE ALL(%(p)s) LIMIT 300""", {"p": pats})
    return [r["code"] for r in rows]


# Пороги RAG (подбор — eval/item_retrieval.py --tune): нижняя граница близости и допуск от лучшего кода
USE_LLM_REWRITE = os.getenv("RAG_LLM_REWRITE", "1") == "1" and llm.enabled()
_defaults = {"floor": "0.80", "delta": "0.02", "head": "0.05"} if USE_LLM_REWRITE else \
            {"floor": "0.86", "delta": "0.03", "head": "0.08"}
SEM_FLOOR = float(os.getenv("RAG_FLOOR", _defaults["floor"]))
SEM_DELTA = float(os.getenv("RAG_DELTA", _defaults["delta"]))
SEM_HEAD = float(os.getenv("RAG_HEAD_BONUS", _defaults["head"]))
SEM_LLM_POOL = int(os.getenv("RAG_LLM_POOL", "15"))


def _semantic_codes(db, p):
    """Кандидаты RAG с надбавкой за совпадение главного слова названия и отбором через LLM."""
    queries = [p.item_phrase]
    if USE_LLM_REWRITE:
        p.item_rewrite = llm.rewrite_item(p.item_phrase)
        if p.item_rewrite and p.item_rewrite.lower() != p.item_phrase.lower():
            queries.append(p.item_rewrite)
    best = {}
    for q in queries:
        for c, s in semantic.search(db, q, k=30):
            best[c] = max(best.get(c, 0.0), s)
    hits = sorted(best.items(), key=lambda x: -x[1])[:30]
    if not hits:
        return []
    info = {r["code"]: r for r in db.query(
        "SELECT code, name_ru, name_kz FROM ref.enstru WHERE code = ANY(%(c)s)", {"c": [c for c, _ in hits]})}
    # «услуги», «работы», «қызмет» — главное слово у сотен кодов, надбавку не дают
    prefixes = [w[:5] for q in queries for w in re.findall(r"[\wәғқңөұүһі-]{3,}", q.lower())
                if not is_stop(w) and not re.match(r"(услуг|работ|товар|қызмет|жұмыс|тауар)", w)]

    def head(code):
        row = info.get(code) or {}
        words_ru = re.findall(r"[\w-]+", (row.get("name_ru") or "").lower())
        words_kz = re.findall(r"[\w-]+", (row.get("name_kz") or "").lower())
        # в русском названии главное слово первое, в казахском — последнее («Шприцті сорғы» — насос)
        heads = ([words_ru[0]] if words_ru else []) + ([words_kz[-1]] if words_kz else [])
        return any(h.startswith(pf) for h in heads for pf in prefixes)

    scored = sorted(((c, s + (SEM_HEAD if head(c) else 0.0)) for c, s in hits), key=lambda x: -x[1])
    top = scored[0][1]
    chosen = [(c, s) for c, s in scored if s >= max(top - SEM_DELTA, SEM_FLOOR)]

    # близости похожих услуг почти равны, порогом их не разделить — окончательный выбор за LLM
    if USE_LLM_REWRITE:
        pool = [(c, s) for c, s in scored if s >= SEM_FLOOR]
        names = tuple(dict.fromkeys((info.get(c) or {}).get("name_ru") for c, _ in pool if info.get(c)))[:SEM_LLM_POOL]
        query = p.item_phrase + (f" ({p.item_rewrite})" if p.item_rewrite and p.item_rewrite != p.item_phrase else "")
        picked = llm.select_items(query, names)
        if picked is not None:   # None — ответ не разобран, остаётся отбор по порогам
            chosen = [(c, s) for c, s in pool if (info.get(c) or {}).get("name_ru") in picked]
            p.item_llm_selected = True
    p.item_matches = [(c, (info.get(c) or {}).get("name_ru"), s) for c, s in chosen]
    return [c for c, _ in chosen]


def resolve_items(db, p):
    """Коды ЕНС ТРУ: явные → лексический поиск по всем словам → RAG → по отдельным словам."""
    if p.enstru:
        p.item_source = "explicit"
        return list(p.enstru)
    if not p.item_terms and not p.item_phrase:
        return []
    codes = _codes_for(db, p.item_terms) if p.item_terms else []
    if not codes and p.synonym_terms and set(p.synonym_terms) != set(p.item_terms):
        codes = _codes_for(db, p.synonym_terms)
    if codes:
        p.item_source = "lexical"
        return codes
    # без содержательных слов (4+ букв) RAG не запускаем — иначе «БИН» находит случайный код
    codes = _semantic_codes(db, p) if p.item_terms else []
    if codes:
        p.item_source = "semantic"
        return codes
    useful = [w for w in p.item_terms if _codes_for(db, [w])]
    p.ignored_terms = [w for w in p.item_terms if w not in useful]
    p.item_terms = useful
    codes = _codes_for(db, useful) if useful else []
    p.item_source = "lexical" if codes else None
    return codes


def common_filters(p, codes, alias, year_col="fin_year", bin_col="customer_bin", code_col="enstru_code",
                   region_col=None, subject_col="subject_type_id"):
    conds, args = [], {}
    if p.subject_type and subject_col:
        conds.append(f"{alias}.{subject_col} = %(stype)s")
        args["stype"] = p.subject_type
    conds.append(f"{alias}.{year_col} = ANY(%(years)s)")
    args["years"] = p.period
    if p.bins:
        conds.append(f"{alias}.{bin_col} = ANY(%(bins)s)")
        args["bins"] = p.bins
    if codes:
        conds.append(f"{alias}.{code_col} = ANY(%(codes)s)")
        args["codes"] = codes
    if p.regions and region_col:
        conds.append(f"{alias}.{region_col} = ANY(%(regions)s)")
        args["regions"] = p.regions
    return " AND ".join(conds), args


def contract_unit_filter(p, codes, args):
    """EXISTS по позициям договора: код и место поставки должны совпасть в одной позиции."""
    conds = []
    if codes:
        conds.append("u.enstru_code = ANY(%(codes)s)")
        args["codes"] = codes
    if p.regions:
        conds.append("left(u.kato_code, 2) = ANY(%(regions)s)")
        args["regions"] = p.regions
    if not conds:
        return ""
    return " AND EXISTS (SELECT 1 FROM core.contract_unit u WHERE u.contract_id = c.id AND " + " AND ".join(conds) + ")"


def city_names(p):
    return ", ".join(CITY_NAMES[r] for r in p.regions)


def data_used_block(p, codes, subjects, lang, entities, extra=None):
    bins = ", ".join(short_name(subjects, b) for b in p.bins) if p.bins else t("all_27", lang)
    if codes:
        items = ", ".join(codes[:8]) + (f" … (+{len(codes) - 8})" if len(codes) > 8 else "")
    else:
        items = t("all_items", lang)
    out = [f"{t('period', lang)}: {years_txt(p)}",
           f"{t('customers', lang)}: {bins}",
           f"{t('items', lang)}: {items}"]
    if p.subject_type:
        out.append(f"{t('subject_type', lang)}: {t(f'st_{p.subject_type}', lang)}")
    if p.regions:
        out.append(f"{t('city', lang)}: {city_names(p)} (КАТО {', '.join(r + '…' for r in p.regions)})")
    if extra:
        out.append(f"{t('filters', lang)}: {extra}")
    if p.item_source == "semantic" and p.item_matches:
        seen, shown = set(), []
        for _, name, s in p.item_matches:
            if name not in seen:
                seen.add(name)
                shown.append(f"«{name}» ({s:.2f})")
        out.append(t("rag_note", lang, phrase=p.item_phrase, n=len(p.item_matches), names=", ".join(shown[:5])))
    if p.ignored_terms:
        out.append(t("ignored_terms", lang, terms=", ".join(p.ignored_terms)))
    out.append(f"{t('entities', lang)}: {entities}")
    return out


def cpi_month(db):
    r = db.one("SELECT to_char(max(period), 'MM.YYYY') AS m FROM ref.cpi")
    return r.get("m") or "—"


UNIT_COLUMNS = [("contract_id", "ID договора"), ("contract_number", "№"), ("sign_date", "Дата"),
                ("customer_bin", "Заказчик"), ("enstru_name", "ТРУ"), ("quantity_base", "Кол-во"),
                ("price_base", "Цена на дату договора, тг"), ("amount", "Сумма без НДС"), ("anno_url", "Ссылка")]
CONTRACT_COLUMNS = [("contract_id", "ID договора"), ("contract_number", "№"), ("sign_date", "Дата"),
                    ("customer_bin", "Заказчик"), ("supplier", "Поставщик"),
                    ("subject_type", "Вид"), ("contract_sum_wnds", "Сумма с НДС"), ("anno_url", "Ссылка")]

SUPPLIER_AND_TYPE = """c.supplier_bin || coalesce(' (' || (SELECT name_ru FROM core.subject sp WHERE sp.bin = c.supplier_bin) || ')', '')
                   AS supplier,
               (SELECT name_ru FROM core.subject sp WHERE sp.bin = c.supplier_bin) AS supplier_name,
               (SELECT name_ru FROM ref.status st WHERE st.kind = 'subject_type' AND st.id = c.subject_type_id) AS subject_type"""

# у договоров без конкурса trdBuyId = 0 — объявления нет
ANNO_URL = f"CASE WHEN c.trd_buy_id > 0 THEN '{PORTAL_ANNO}' || c.trd_buy_id END"


def top_units(db, w, args, k, order="u.price_base * u.quantity_base DESC"):
    return db.query(f"""
        SELECT u.contract_id, u.contract_number, u.sign_date::date AS sign_date, u.customer_bin, u.enstru_name,
               u.quantity_base, u.price_base, u.price_base * u.quantity_base AS amount, u.anno_url
        FROM mart.unit_price u WHERE u.is_target AND {w} ORDER BY {order} LIMIT {int(k)}""", args)


def top_contracts(db, w, args, k):
    return db.query(f"""
        SELECT c.id AS contract_id, c.contract_number, c.sign_date::date AS sign_date, c.customer_bin,
               c.supplier_bin, c.contract_sum_wnds, {ANNO_URL} AS anno_url,
               {SUPPLIER_AND_TYPE}
        FROM core.contract c LEFT JOIN ref.status s ON s.kind = 'contract' AND s.id = c.status_id
        WHERE c.is_target AND NOT coalesce(c.is_deleted, false) AND coalesce(s.name_ru, '') !~* %(bad)s AND {w}
        ORDER BY c.contract_sum_wnds DESC NULLS LAST LIMIT {int(k)}""", {**args, "bad": BAD_CONTRACT_STATUS})


def dq_units(db, w, args, alias="u", table="mart.unit_price"):
    q = db.one(f"""SELECT count(*) AS n,
                          count(*) FILTER (WHERE {alias}.unit_source <> 'plan') AS unit_inferred,
                          count(*) FILTER (WHERE {alias}.kato_code IS NULL) AS no_kato,
                          count(*) FILTER (WHERE {alias}.cpi_carried) AS cpi_carried
                   FROM {table} {alias} WHERE {w}""", args)
    n = q.get("n") or 0
    if not n:
        return []
    share = lambda x: f"{(q.get(x) or 0) / n * 100:.1f}%"
    return [f"Качество данных ({n} позиций): единица измерения выведена по правилам — {share('unit_inferred')}, "
            f"нет места поставки (КАТО) — {share('no_kato')}, ИПЦ за месяц подписания не опубликован — "
            f"{share('cpi_carried')}."]


# ---------- аномалии цены ----------

def price_anomaly(db, p, subjects):
    lang = p.lang
    codes = resolve_items(db, p)
    w, args = common_filters(p, codes, "u", region_col="region_code")
    thr = p.threshold / 100
    args.update(thr=thr, k=p.top_k)
    cond_flag = "ratio > 1 + %(thr)s AND ratio <= 10" if p.direction == "up" else "ratio < 1 - %(thr)s AND ratio >= 0.1"
    base = f"""
        WITH s AS (
            SELECT enstru_code, base_unit_code, n, n_customers, weighted_avg, median
            FROM mart.enstru_price_stats WHERE n >= 5 AND n_customers >= 3
        ), base AS (
            SELECT u.unit_id, u.contract_id, u.contract_number, u.trd_buy_id, u.customer_bin, u.supplier_bin,
                   u.sign_date::date AS sign_date, u.enstru_code, u.enstru_name, u.base_unit_code,
                   un.name_ru AS unit_name, u.quantity_base, u.price_real, u.unit_source,
                   s.weighted_avg, s.median, s.n, s.n_customers,
                   u.price_real / s.weighted_avg AS ratio, u.anno_url
            FROM mart.unit_price u
            JOIN s USING (enstru_code, base_unit_code)
            LEFT JOIN ref.units un ON un.code = u.base_unit_code
            WHERE u.is_target AND {w}
        )"""
    agg = db.one(base + f"""
        SELECT count(*) AS n_all,
               count(*) FILTER (WHERE {cond_flag}) AS n_flag,
               count(*) FILTER (WHERE ratio > 10 OR ratio < 0.1) AS n_err,
               count(DISTINCT enstru_code) AS n_codes,
               sum((price_real - weighted_avg) * quantity_base) FILTER (WHERE {cond_flag}) AS excess,
               sum(price_real * quantity_base) AS amount_all,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY (ratio - 1) * 100) FILTER (WHERE {cond_flag}) AS med_dev,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY n) FILTER (WHERE {cond_flag}) AS med_n,
               count(*) FILTER (WHERE {cond_flag} AND unit_source <> 'plan') AS n_inferred_unit
        FROM base""", args)
    order = "(price_real - weighted_avg) * quantity_base DESC" if p.direction == "up" else "ratio ASC"
    rows = db.query(base + f"""
        SELECT *, (ratio - 1) * 100 AS dev_pct, (price_real - weighted_avg) * quantity_base AS excess
        FROM base WHERE {cond_flag} ORDER BY {order} LIMIT %(k)s""", args)

    r = Result(qtype="anomaly", n_analyzed=agg.get("n_all"), n_examples_total=agg.get("n_flag"))
    n_all, n_flag = agg.get("n_all") or 0, agg.get("n_flag") or 0
    sign = ">" if p.direction == "up" else "<"
    if lang == "ru":
        r.summary = [f"Найдено {n_flag} позиций договоров из {n_all} проанализированных с отклонением цены "
                     f"{sign} {p.threshold:g}% от средневзвешенной по своему коду ЕНС ТРУ "
                     f"({n_flag / n_all * 100 if n_all else 0:.1f}%)."]
        if p.direction == "up" and agg.get("excess"):
            r.summary.append(f"Оценка суммы превышения над средневзвешенной ценой: {money(agg['excess'])}.")
    else:
        r.summary = [f"Талданған {n_all} позицияның {n_flag}-інде баға өз ЕНС ТРУ коды бойынша орташа өлшемді "
                     f"бағадан {sign} {p.threshold:g}% ауытқиды ({n_flag / n_all * 100 if n_all else 0:.1f}%)."]
        if p.direction == "up" and agg.get("excess"):
            r.summary.append(f"Орташа өлшемді бағадан асқан соманың бағасы: {money(agg['excess'])}.")
    r.data_used = data_used_block(p, codes, subjects, lang,
                                  "mart.unit_price (позиции договоров), mart.enstru_price_stats",
                                  f"порог {sign}{p.threshold:g}%, коды с N ≥ 5 сделок и ≥ 3 заказчиков")
    r.comparison = [
        f"Кодов ЕНС ТРУ в анализе: {agg.get('n_codes')}; сумма проанализированных позиций: {money(agg.get('amount_all'))}",
        f"Медиана отклонения среди найденных: {pct(agg.get('med_dev'))}",
        f"Исключено как вероятная ошибка данных (цена ×10 от средневзвешенной): {agg.get('n_err')}",
    ]
    if len(codes) == 1:
        s = db.one("SELECT * FROM mart.enstru_price_stats WHERE enstru_code = %(c)s ORDER BY n DESC LIMIT 1", {"c": codes[0]})
        if s:
            r.comparison.insert(0, f"Код {codes[0]}: средневзвешенная {num(s['weighted_avg'])} тг, медиана {num(s['median'])} тг, "
                                   f"p25–p75: {num(s['p25'])}–{num(s['p75'])} тг, N = {s['n']} сделок, {s['n_customers']} заказчиков")
            r.n_compared = s["n"]
    r.metric = [
        "Отклонение D = (P_позиции / P_средневзвешенная − 1) × 100%,",
        "P_средневзвешенная = Σ(цена × количество) / Σ количество по всем сделкам кода ЕНС ТРУ и базовой единицы "
        "(27 организаций + рынок по 100 крупнейшим кодам).",
        t("price_note", lang, cpi=cpi_month(db)),
    ]
    r.data_quality = dq_units(db, f"u.is_target AND {w}", args)
    med_n = agg.get("med_n") or 0
    r.confidence = "high" if med_n >= 30 else "medium" if med_n >= 10 else "low"
    r.confidence_why = f"медиана размера выборки по найденным кодам — {num(med_n, 0)} сделок"
    r.limitations = [
        "Средневзвешенная считается по всем сделкам кода, включая саму позицию; под одним кодом могут быть разные "
        "модели и комплектации товара — проверяйте наименование.",
        f"У {agg.get('n_inferred_unit')} найденных позиций единица измерения выведена (не из пункта плана).",
        "Анализ охватывает только позиции с известной единицей и кодом ЕНС ТРУ.",
    ]
    r.columns = [("contract_id", "ID договора"), ("contract_number", "№ договора"), ("sign_date", "Дата"),
                 ("customer_bin", "БИН заказчика"), ("enstru_name", "ТРУ"), ("unit_name", "Ед."),
                 ("price_real", "Цена"), ("weighted_avg", "Средневзв."), ("dev_pct", "D"), ("n", "N"),
                 ("excess", "Превышение"), ("anno_url", "Ссылка")]
    r.examples = rows
    return r


# ---------- справедливая цена ----------

def fairness(db, p, subjects):
    lang = p.lang
    if not p.lot_ref:
        return fairness_by_filter(db, p, subjects)
    ref = p.lot_ref
    lots = db.query("""
        SELECT l.id, l.lot_number, l.name_ru, l.customer_bin, l.trd_buy_id, tb.number_anno, l.amount, l.count
        FROM core.lot l LEFT JOIN core.trd_buy tb ON tb.id = l.trd_buy_id
        WHERE l.id::text = %(r)s OR l.lot_number = %(r)s OR tb.number_anno = %(r)s OR l.trd_buy_id::text = %(r)s
        LIMIT 50""", {"r": ref})
    lot_ids = [x["id"] for x in lots]
    sql = """
        SELECT f.*, u.price_base, un.name_ru AS unit_name
        FROM mart.fair_price f
        JOIN mart.unit_price u USING (unit_id)
        LEFT JOIN ref.units un ON un.code = f.base_unit_code
        WHERE {cond} ORDER BY f.unit_id"""
    rows = db.query(sql.format(cond="f.lot_id = ANY(%(ids)s)"), {"ids": lot_ids}) if lot_ids else []
    if not rows:  # может быть номер или ID договора
        rows = db.query(sql.format(cond="f.contract_id::text = %(r)s OR f.contract_number = %(r)s"), {"r": ref})

    r = Result(qtype="fairness")
    if not rows:
        r.confidence = "none"
        if lots:
            lt = lots[0]
            r.summary = [L(lang,
                f"Лот {lt['lot_number']} («{lt['name_ru']}», заказчик {lt['customer_bin']}) найден, но по нему "
                f"нет заключённого договора с ценой за единицу — сравнивать нечего.",
                f"{lt['lot_number']} лоты («{lt['name_ru']}», тапсырыс беруші {lt['customer_bin']}) табылды, бірақ ол "
                f"бойынша бірлік бағасы бар жасалған келісімшарт жоқ — салыстыруға ештеңе жоқ.")]
        else:
            r.summary = [L(lang,
                f"Лот, объявление или договор «{ref}» не найден в базе 27 организаций за 2024–2026 годы.",
                f"«{ref}» лоты, хабарландыруы немесе келісімшарты 2024–2026 жылдардағы 27 ұйым базасында табылмады.")]
        r.limitations = ["Fair Price считается по позициям заключённых договоров с известными кодом ЕНС ТРУ и единицей."]
        return r

    x = rows[0]
    priced = [row for row in rows if row["tier"]]
    r.n_analyzed = len(rows)
    r.n_compared = x["n"]
    head = lots[0] if lots else {}
    if head:
        cids = ", ".join(str(c) for c in sorted({row["contract_id"] for row in rows}))
        obj = L(lang, f"Лот {head['lot_number']} (ID {head['id']}, договор ID {cids})",
                f"Лот {head['lot_number']} (ID {head['id']}, келісімшарт ID {cids})")
    else:
        obj = L(lang, f"Договор {x['contract_number']} (ID {x['contract_id']})",
                f"Келісімшарт {x['contract_number']} (ID {x['contract_id']})")
    if not priced:
        r.summary = [L(lang,
            f"{obj}: аналогов у других заказчиков недостаточно (нужно ≥ 5 сделок от ≥ 3 заказчиков) — "
            f"справедливую цену оценить нельзя.",
            f"{obj}: басқа тапсырыс берушілерде аналогтар жеткіліксіз (≥ 3 тапсырыс берушіден ≥ 5 мәміле қажет) — "
            f"әділ бағаны бағалау мүмкін емес.")]
        r.confidence = "none"
    elif len(rows) == 1 or len(priced) == 1:
        y = priced[0]
        r.summary = [L(lang,
            f"{obj}, «{y['enstru_name']}»: цена {num(y['price_base'])} тг/{y['unit_name']} против справедливой "
            f"{num(y['p_fair_nominal'])} тг (D = {pct(y['deviation_pct'])}). Вердикт: цена {verdict(y['price_verdict'], lang)}.",
            f"{obj}, «{y['enstru_name']}»: бағасы {num(y['price_base'])} тг/{y['unit_name']}, әділ баға "
            f"{num(y['p_fair_nominal'])} тг (D = {pct(y['deviation_pct'])}). Қорытынды: баға {verdict(y['price_verdict'], lang)}.")]
        r.confidence = y["confidence"]
    else:
        over = sum(1 for y in priced if y["price_verdict"] == "overpriced")
        r.summary = [L(lang,
            f"{obj}: {len(rows)} позиций, оценено {len(priced)}; завышено со статистическим выбросом — {over}.",
            f"{obj}: {len(rows)} позиция, бағаланғаны {len(priced)}; статистикалық шығыңқы мәнмен асырылғаны — {over}.")]
        r.confidence = min((y["confidence"] for y in priced), key=["none", "low", "medium", "high"].index)

    r.data_used = data_used_block(p, [x["enstru_code"]], subjects, lang,
                                  "mart.fair_price, mart.unit_price, mart.region_coef, mart.season_coef",
                                  f"аналоги: тот же код и единица, другие заказчики, ±24 мес. от {x['sign_date']:%d.%m.%Y}")
    for y in rows:
        if not y["tier"]:
            r.comparison.append(f"Позиция {y['unit_id']}: аналогов недостаточно.")
            continue
        r.comparison.append(
            f"Позиция {y['unit_id']} ({y['enstru_name']}): медиана аналогов {num(y['p_median'])} тг "
            f"(p25–p75: {num(y['p25'])}–{num(y['p75'])}, MAD {num(y['mad'])}), N = {y['n']} сделок, "
            f"{y['n_customers']} заказчиков, выборка — {tier(y['tier'], lang)}; K_region = {num(y['k_region'], 3)}, "
            f"K_season = {num(y['k_season'], 3)}; z = {num(y['robust_z'], 1)}, IQR: {y['iqr_flag'] or 'в пределах'}.")
    r.metric = [
        "P_fair = медиана цен аналогов × K_region × K_season;",
        "аналоги ищутся по ступеням: тот же город (КАТО 4 знака) → область (2 знака) → страна × K_region; "
        "на ступени нужно ≥ 5 сделок от ≥ 3 заказчиков;",
        "D = (P_actual / P_fair − 1) × 100%; z = (P − P_fair) / (1.4826 × MAD);",
        "вердикт «завышена»: D > 30% и (z ≥ 3.5 или цена выше p75 + 1.5·IQR).",
        t("price_note", lang, cpi=cpi_month(db)),
    ]
    r.confidence_why = (f"ступень «{tier(x['tier'], lang)}», N = {x['n']}, разброс IQR/медиана = {num(x['rel_iqr'])}"
                        if x["tier"] else "аналогов недостаточно")
    if x["base_unit_code"] in ("5114", "980"):
        r.confidence_why += "; единица «Одна услуга»/«Работа» — объём услуги в каждом договоре свой"
    if x["tier"] and x["tier"] != "city":
        r.limitations.append("В том же городе поставки аналогов меньше 5 — использована более широкая выборка.")
    if x.get("unit_source") and x["unit_source"] != "plan":
        r.limitations.append("Единица измерения позиции выведена по правилам (не из пункта плана).")
    if x.get("cpi_carried"):
        r.limitations.append("ИПЦ за месяц подписания ещё не опубликован — взят последний известный.")
    r.limitations.append("Под одним кодом ЕНС ТРУ могут быть разные модели/комплектации — сравнение по коду, а не по характеристикам.")
    src = {"plan": "из пункта плана", "customer_plan": "выведена по плану заказчика",
           "enstru_dominant": "выведена по типичной единице кода"}
    r.data_quality = [f"Качество данных позиции: единица измерения — {src.get(x.get('unit_source'), '—')}; "
                      f"место поставки (КАТО) — {x.get('kato_code') or 'не указано'}; "
                      f"ИПЦ — {'перенесён последний известный' if x.get('cpi_carried') else 'за месяц подписания'}; "
                      f"выборка аналогов: N = {x.get('n') or 0}, IQR/медиана = {num(x.get('rel_iqr'))}."]

    # сначала сама закупка, затем аналоги (со ссылкой — выше)
    own = [{"role": L(lang, "оцениваемая", "бағаланатын"), "contract_id": y["contract_id"],
            "contract_number": y["contract_number"], "sign_date": y["sign_date"], "customer_bin": y["customer_bin"],
            "kato_code": y["kato_code"], "price_base": y["price_base"], "anno_url": y["anno_url"]} for y in rows]
    ids = sorted({c for y in priced for c in (y["analog_contract_ids"] or [])})
    analogs = []
    if ids:
        analogs = db.query("""
            SELECT u.contract_id, u.contract_number, u.sign_date::date AS sign_date, u.customer_bin, u.kato_code,
                   u.enstru_name, u.price_base, u.anno_url
            FROM mart.unit_price u
            WHERE u.contract_id = ANY(%(ids)s) AND u.enstru_code = %(c)s AND u.base_unit_code = %(b)s
            ORDER BY u.anno_url IS NULL, u.sign_date DESC LIMIT %(k)s""",
            {"ids": ids, "c": x["enstru_code"], "b": x["base_unit_code"], "k": p.top_k})
        for a in analogs:
            a["role"] = L(lang, "аналог", "аналог")
    r.examples = own + analogs
    r.columns = [("role", "Роль"), ("contract_id", "ID договора"), ("contract_number", "№"), ("sign_date", "Дата"),
                 ("customer_bin", "Заказчик"), ("kato_code", "КАТО"), ("price_base", "Цена, тг"), ("anno_url", "Ссылка")]
    if any(a["anno_url"] is None for a in analogs):
        r.limitations.append("У части договоров-аналогов из рыночной выборки нет ссылки на объявление: "
                             "в выгрузке market.py не было поля trdBuyId (добавлено, появится после перезагрузки рынка).")
    return r


def fairness_by_filter(db, p, subjects):
    lang = p.lang
    codes = resolve_items(db, p)
    w, args = common_filters(p, codes, "f", region_col="region_code")
    args.update(k=p.top_k, thr=p.threshold)
    dist = db.query(f"""SELECT price_verdict, count(*) AS n FROM mart.fair_price f WHERE {w}
                        GROUP BY 1 ORDER BY 2 DESC""", args)
    total = sum(d["n"] for d in dist)
    d = {x["price_verdict"]: x["n"] for x in dist}
    priced_n = total - d.get("insufficient_data", 0)
    # порог из вопроса — отбор по D, иначе по вердикту «завышена»
    if p.threshold_given:
        sign = ">" if p.direction == "up" else "<"
        cond = ("f.deviation_pct > %(thr)s" if p.direction == "up" else "f.deviation_pct < -%(thr)s") + \
               " AND f.data_flag IS NULL"
    else:
        cond = "f.price_verdict = 'overpriced'"
    n_sel = db.one(f"SELECT count(*) AS n FROM mart.fair_price f WHERE {w} AND {cond}", args).get("n")
    rows = db.query(f"""
        SELECT f.unit_id, f.contract_id, f.contract_number, f.sign_date::date AS sign_date, f.customer_bin, f.enstru_name,
               f.p_actual, f.p_fair, f.deviation_pct, f.robust_z, f.tier, f.n, f.confidence, f.anno_url
        FROM mart.fair_price f WHERE {w} AND {cond}
        ORDER BY (f.p_actual - f.p_fair) * f.quantity_base {'DESC' if p.direction == 'up' else 'ASC'} LIMIT %(k)s""", args)
    r = Result(qtype="fairness", n_analyzed=total)
    if p.threshold_given:
        r.summary = [L(lang,
            f"Из {total} позиций договоров по Fair Price оценено {priced_n}; цена {'выше' if p.direction == 'up' else 'ниже'} "
            f"справедливой более чем на {p.threshold:g}% — у {n_sel} позиций (без вероятных ошибок данных).",
            f"{total} позицияның {priced_n}-і Fair Price бойынша бағаланды; бағасы әділ бағадан {p.threshold:g}%-дан "
            f"{'артық' if p.direction == 'up' else 'төмен'} — {n_sel} позиция (деректер қателері ықтимал позицияларсыз).")]
    else:
        r.summary = [L(lang,
            f"Из {total} позиций договоров оценено по Fair Price {priced_n}; "
            f"справедливых — {d.get('fair', 0)}, завышенных со статистическим выбросом — {d.get('overpriced', 0)}.",
            f"{total} позицияның {priced_n}-і Fair Price бойынша бағаланды; әділ — {d.get('fair', 0)}, "
            f"статистикалық шығыңқы мәнмен асырылған — {d.get('overpriced', 0)}.")]
    r.data_used = data_used_block(p, codes, subjects, lang, "mart.fair_price",
                                  f"D {sign} {p.threshold:g}%" if p.threshold_given else "вердикт «завышена»")
    r.comparison = [f"{verdict(k, lang)}: {v}" for k, v in d.items()]
    r.metric = ["P_fair = медиана аналогов × K_region × K_season; вердикт «завышена»: D > 30% и (z ≥ 3.5 или выход за IQR).",
                t("price_note", lang, cpi=cpi_month(db))]
    r.confidence = "medium"
    r.confidence_why = "сводка по многим позициям; уверенность по каждой — в колонке «Увер.»"
    r.limitations = ["Позиции без достаточного числа аналогов (insufficient_data) не оцениваются."]
    r.columns = [("contract_id", "ID договора"), ("sign_date", "Дата"), ("customer_bin", "Заказчик"), ("enstru_name", "ТРУ"),
                 ("p_actual", "Цена"), ("p_fair", "P_fair"), ("deviation_pct", "D"), ("robust_z", "z"), ("n", "N"),
                 ("confidence", "Увер."), ("anno_url", "Ссылка")]
    r.examples, r.n_examples_total = rows, n_sel
    r.data_quality = dq_units(db, w, args, alias="f", table="mart.fair_price")
    return r


# ---------- объёмы ----------

def volume(db, p, subjects):
    lang = p.lang
    codes = resolve_items(db, p)
    py = p.years or [2025, 2026]          # для 2024 нет предыдущих лет
    p2 = type(p)(**{**p.__dict__, "years": [y for y in py if y > 2024] or [2025, 2026]})
    w, args = common_filters(p2, codes, "a", year_col="year", subject_col=None)
    if p.subject_type:   # в qty_anomaly вида нет — берём вид кода
        w += " AND a.enstru_code IN (SELECT code FROM ref.enstru WHERE subject_type_id = %(stype)s)"
        args["stype"] = p.subject_type
    args["k"] = p.top_k
    dist = db.query(f"SELECT verdict, count(*) AS n FROM mart.qty_anomaly a WHERE {w} GROUP BY 1", args)
    d = {x["verdict"]: x["n"] for x in dist}
    total = sum(d.values())
    rows = db.query(f"""
        SELECT a.*, e.name_ru AS enstru_name, un.name_ru AS unit_name,
               (SELECT array_agg(id ORDER BY id) FROM (SELECT id FROM core.plan_point pp
                 WHERE pp.subject_bin = a.customer_bin AND pp.enstru_code = a.enstru_code AND pp.plan_year = a.year
                 LIMIT 5) s) AS plan_ids
        FROM mart.qty_anomaly a
        LEFT JOIN ref.enstru e ON e.code = a.enstru_code
        LEFT JOIN ref.units un ON un.code = a.base_unit_code
        WHERE {w} AND a.verdict IN ('anomaly', 'anomaly_no_peers')
        ORDER BY (a.verdict = 'anomaly') DESC, a.excess_amount_est DESC NULLS LAST LIMIT %(k)s""", args)
    excess = db.one(f"""SELECT sum(excess_amount_est) AS s FROM mart.qty_anomaly a
                        WHERE {w} AND verdict = 'anomaly'""", args).get("s")

    r = Result(qtype="volume", n_analyzed=total, n_examples_total=d.get("anomaly", 0) + d.get("anomaly_no_peers", 0))
    r.summary = [L(lang,
        f"Из {total} сочетаний «заказчик × код ЕНС ТРУ × год» с историей нетипичное завышение количества "
        f"(рост ≥ 2 раз, выбивается из тренда других заказчиков) — {d.get('anomaly', 0)}; "
        f"ещё {d.get('anomaly_no_peers', 0)} кандидатов, которые не с кем сравнить.",
        f"Тарихы бар {total} «тапсырыс беруші × ЕНС ТРУ коды × жыл» үйлесімінің ішінде санның әдеттен тыс "
        f"асырылуы (≥ 2 есе өсім, басқа тапсырыс берушілердің трендінен ерекшеленеді) — {d.get('anomaly', 0)}; "
        f"салыстыратын ешкім жоқ тағы {d.get('anomaly_no_peers', 0)} үміткер.")]
    if excess:
        r.summary.append(L(lang, f"Оценка суммы сверх обычного объёма по подтверждённым аномалиям: {money(excess)}.",
                           f"Расталған аномалиялар бойынша әдеттегі көлемнен асқан соманың бағасы: {money(excess)}."))
    r.data_used = data_used_block(p2, codes, subjects, lang, "mart.qty_anomaly, mart.qty_by_year, core.plan_point",
                                  "плановое количество; без отменённых и старых версий пунктов плана")
    r.comparison = [f"{verdict(k, lang)}: {v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1])]
    r.metric = [
        "growth = плановое количество года / медиана планового количества предыдущих лет (тот же заказчик, код, единица);",
        "z = (ln growth − медиана ln growth у других заказчиков кода в том же году) / (1.4826 × MAD);",
        "аномалия: growth ≥ 2, сумма плана ≥ 1 млн тг и z ≥ 3.5; если других заказчиков < 5 — «кандидат».",
    ]
    r.confidence = "medium"
    r.confidence_why = "история — 1–2 года (2024–2025); уверенность по каждой строке — в колонке «Увер.»"
    r.limitations = [
        "Данные начинаются с 2024 года: для 2025 база сравнения — один год.",
        "Используется план (годовой), а не договоры: договоры 2026 года есть только до октября.",
        "Для услуг («Одна услуга») рост количества часто означает разбиение на несколько договоров.",
    ]
    if p.regions:
        r.limitations.append("Фильтр по городу здесь не применяется: объёмы плана считаются по заказчику в целом.")
    r.group_columns = [("customer_bin", "Заказчик"), ("year", "Год"), ("enstru_name", "ТРУ"), ("unit_name", "Ед."),
                       ("base_qty", "Было (медиана)"), ("plan_qty", "План"), ("growth", "Рост, ×"),
                       ("peer_median_growth", "Рост у других"), ("robust_z", "z"), ("excess_amount_est", "Сверх нормы"),
                       ("verdict", "Вердикт"), ("plan_ids", "ID пунктов плана")]
    r.groups = rows
    if rows:
        keys = {"b": [x["customer_bin"] for x in rows], "e": [x["enstru_code"] for x in rows],
                "y": [x["year"] for x in rows]}
        r.examples = top_units(db, "(u.customer_bin, u.enstru_code, u.fin_year) IN "
                                   "(SELECT * FROM unnest(%(b)s::text[], %(e)s::text[], %(y)s::int[]))", keys, p.top_k)
        r.columns = UNIT_COLUMNS
    return r


# ---------- поиск ----------

def contract_card(db, p, subjects):
    """Карточка договора по номеру/ID договора, лота или объявления."""
    lang, ref = p.lang, p.lot_ref
    rows = db.query("""
        SELECT c.id AS contract_id, c.contract_number, c.sign_date::date AS sign_date, c.fin_year, c.customer_bin,
               c.supplier_bin, c.contract_sum_wnds, c.contract_sum, s.name_ru AS status, tm.name_ru AS method,
               (SELECT string_agg(DISTINCT coalesce(u.name_ru, e.name_ru), '; ')
                FROM core.contract_unit u LEFT JOIN ref.enstru e ON e.code = u.enstru_code
                WHERE u.contract_id = c.id) AS items,
               (SELECT count(*) FROM core.contract_unit u WHERE u.contract_id = c.id) AS n_units,
               c.trd_buy_name, c.fakt_sum,
               {anno} AS anno_url,
               {sup}
        FROM core.contract c
        LEFT JOIN ref.status s ON s.kind = 'contract' AND s.id = c.status_id
        LEFT JOIN ref.trade_method tm ON tm.id = c.trade_method_id
        WHERE c.contract_number = %(r)s OR c.id::text = %(r)s
           OR c.trd_buy_id IN (SELECT trd_buy_id FROM core.lot WHERE lot_number = %(r)s OR id::text = %(r)s)
        ORDER BY c.sign_date DESC NULLS LAST LIMIT 20""".format(anno=ANNO_URL, sup=SUPPLIER_AND_TYPE), {"r": ref})
    r = Result(qtype="search", n_analyzed=len(rows), n_examples_total=len(rows))
    if not rows:
        r.confidence = "none"
        r.summary = [t("not_found_ref", lang, ref=ref)]
        r.limitations = ["В базе — договоры 27 организаций из ТЗ за 2024–2026 годы."]
        return r
    x = rows[0]
    supplier = f"{x['supplier_bin']} ({x['supplier_name']})" if x.get("supplier_name") else x["supplier_bin"]
    r.summary = [L(lang,
        f"Договор {x['contract_number']} (ID {x['contract_id']}) от {x['sign_date']:%d.%m.%Y}: заказчик "
        f"{short_name(subjects, x['customer_bin'])}, поставщик {supplier}, сумма {money(x['contract_sum_wnds'])} "
        f"с НДС, статус «{x['status']}», способ закупки «{x['method']}».",
        f"{x['contract_number']} келісімшарты (ID {x['contract_id']}), {x['sign_date']:%d.%m.%Y}: тапсырыс беруші "
        f"{short_name(subjects, x['customer_bin'])}, жеткізуші {supplier}, сомасы {money(x['contract_sum_wnds'])} "
        f"(ҚҚС-пен), мәртебесі «{x['status']}», сатып алу тәсілі «{x['method']}».")]
    if len(rows) > 1:
        r.summary.append(L(lang, f"По номеру «{ref}» найдено договоров: {len(rows)} — все в таблице ниже.",
                           f"«{ref}» нөмірі бойынша табылған келісімшарттар: {len(rows)} — барлығы төмендегі кестеде."))
    r.data_used = [f"{t('entities', lang)}: core.contract, core.contract_unit, ref.status, ref.trade_method",
                   f"Объявление: {x.get('trd_buy_name') or '—'}; вид предмета закупки: {x.get('subject_type') or '—'}",
                   f"Позиций в договоре: {x['n_units']}; ТРУ: {x['items']}"]
    r.comparison = [f"Сумма без НДС: {money(x['contract_sum'])}; фактически исполнено: {money(x.get('fakt_sum'))}"]
    r.metric = ["Прямая выборка по номеру/ID из данных OWS v3, без расчётов."]
    r.confidence, r.confidence_why = "high", "данные договора из API без преобразований"
    r.columns = [("contract_id", "ID договора"), ("contract_number", "№"), ("sign_date", "Дата"),
                 ("customer_bin", "Заказчик"), ("supplier", "Поставщик"), ("subject_type", "Вид"),
                 ("contract_sum_wnds", "Сумма с НДС"), ("items", "ТРУ"), ("status", "Статус"), ("anno_url", "Ссылка")]
    r.examples = rows
    return r


def search(db, p, subjects):
    lang = p.lang
    if p.lot_ref:
        return contract_card(db, p, subjects)
    codes = resolve_items(db, p)
    w, args = common_filters(p, [], "c")
    w += contract_unit_filter(p, codes, args)
    args.update(k=p.top_k, bad=BAD_CONTRACT_STATUS)
    base = f"""
        WITH c AS (
            SELECT c.*, s.name_ru AS status, tm.name_ru AS method
            FROM core.contract c
            LEFT JOIN ref.status s ON s.kind = 'contract' AND s.id = c.status_id
            LEFT JOIN ref.trade_method tm ON tm.id = c.trade_method_id
            WHERE c.is_target AND NOT coalesce(c.is_deleted, false)
              AND coalesce(s.name_ru, '') !~* %(bad)s
        ) """
    agg = db.one(base + f"""SELECT count(*) AS n, sum(contract_sum_wnds) AS s,
                                   percentile_cont(0.5) WITHIN GROUP (ORDER BY contract_sum_wnds) AS med,
                                   count(DISTINCT supplier_bin) AS n_sup
                            FROM c WHERE {w}""", args)
    rows = db.query(base + f"""
        SELECT c.id AS contract_id, c.contract_number, c.sign_date::date AS sign_date, c.customer_bin, c.supplier_bin,
               c.contract_sum_wnds, c.status, c.method,
               (SELECT string_agg(DISTINCT coalesce(u.name_ru, e.name_ru), '; ')
                FROM core.contract_unit u LEFT JOIN ref.enstru e ON e.code = u.enstru_code
                WHERE u.contract_id = c.id) AS items,
               {ANNO_URL} AS anno_url,
               {SUPPLIER_AND_TYPE}
        FROM c WHERE {w} ORDER BY c.contract_sum_wnds DESC NULLS LAST LIMIT %(k)s""", args)
    r = Result(qtype="search", n_analyzed=agg.get("n"), n_examples_total=agg.get("n"))
    r.summary = [L(lang,
        f"Найдено {agg.get('n')} договоров на общую сумму {money(agg.get('s'))} (с НДС), {agg.get('n_sup')} поставщиков.",
        f"{agg.get('n')} келісімшарт табылды, жалпы сомасы {money(agg.get('s'))} (ҚҚС-пен), {agg.get('n_sup')} жеткізуші.")]
    r.data_used = data_used_block(p, codes, subjects, lang, "core.contract, core.contract_unit",
                                  "без незаключённых, проектов и старых версий изменённых договоров")
    r.comparison = [f"Медианная сумма договора: {money(agg.get('med'))}"]
    r.metric = ["Прямая выборка договоров по фильтрам; суммы — contractSumWnds из OWS v3."]
    r.confidence, r.confidence_why = "high", "прямая выборка из данных API без расчётов"
    r.limitations = ["Только 27 организаций из ТЗ и только 2024–2026 годы."]
    r.columns = [("contract_id", "ID договора"), ("contract_number", "№"), ("sign_date", "Дата"),
                 ("customer_bin", "Заказчик"), ("supplier", "Поставщик"), ("subject_type", "Вид"),
                 ("contract_sum_wnds", "Сумма с НДС"), ("items", "ТРУ"), ("status", "Статус"), ("anno_url", "Ссылка")]
    r.examples = rows
    return r


# ---------- сравнение ----------

def compare(db, p, subjects):
    lang = p.lang
    codes = resolve_items(db, p)
    r = Result(qtype="compare")
    if codes and (len(p.regions) >= 2 or len(p.years) >= 2):
        return compare_prices_by(db, p, subjects, codes, "region" if len(p.regions) >= 2 else "year")
    if codes:   # цены по заказчикам
        w, args = common_filters(p, codes, "u", region_col="region_code")
        rows = db.query(f"""
            SELECT u.customer_bin, u.enstru_code, max(u.enstru_name) AS enstru_name, u.base_unit_code,
                   max(un.name_ru) AS unit_name, count(*) AS n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY u.price_real) AS median,
                   sum(u.price_real * u.quantity_base) / sum(u.quantity_base) AS wavg,
                   max(s.weighted_avg) AS market_wavg, max(s.n) AS market_n
            FROM mart.unit_price u
            LEFT JOIN mart.enstru_price_stats s USING (enstru_code, base_unit_code)
            LEFT JOIN ref.units un ON un.code = u.base_unit_code
            WHERE u.is_target AND {w}
            GROUP BY u.customer_bin, u.enstru_code, u.base_unit_code
            ORDER BY u.enstru_code, wavg DESC""", args)
        for x in rows:
            x["vs_market"] = (x["wavg"] / x["market_wavg"] - 1) * 100 if x["market_wavg"] else None
        r.n_analyzed = sum(x["n"] for x in rows)
        r.n_compared = max((x["market_n"] or 0 for x in rows), default=None)
        if rows:
            # max/min — внутри самой частой пары «код + единица»
            pairs = {(x["enstru_code"], x["base_unit_code"]) for x in rows}
            main_pair = max(pairs, key=lambda c: sum(y["n"] for y in rows if (y["enstru_code"], y["base_unit_code"]) == c))
            main = main_pair[0]
            sub = [x for x in rows if (x["enstru_code"], x["base_unit_code"]) == main_pair]
            hi, lo = max(sub, key=lambda x: x["wavg"]), min(sub, key=lambda x: x["wavg"])
            n_cust = len({x["customer_bin"] for x in rows})
            r.summary = [L(lang,
                f"Сравнено {n_cust} заказчиков по {len(codes)} кодам ЕНС ТРУ. По основному коду {main} "
                f"самая высокая средневзвешенная цена — у {short_name(subjects, hi['customer_bin'])}: "
                f"{num(hi['wavg'])} тг/{hi['unit_name']}, самая низкая — у {short_name(subjects, lo['customer_bin'])}: "
                f"{num(lo['wavg'])} тг.",
                f"{n_cust} тапсырыс беруші {len(codes)} ЕНС ТРУ коды бойынша салыстырылды. Негізгі {main} коды бойынша "
                f"ең жоғары орташа өлшемді баға — {short_name(subjects, hi['customer_bin'])}: {num(hi['wavg'])} "
                f"тг/{hi['unit_name']}, ең төменгі — {short_name(subjects, lo['customer_bin'])}: {num(lo['wavg'])} тг.")]
        else:
            r.summary = [L(lang, "По заданным фильтрам нет позиций с ценой за единицу.",
                           "Берілген сүзгілер бойынша бірлік бағасы бар позициялар жоқ.")]
        r.comparison = [f"Рыночная средневзвешенная по коду {x['enstru_code']}: {num(x['market_wavg'])} тг (N = {x['market_n']})"
                        for x in {x["enstru_code"]: x for x in rows}.values()][:5]
        r.metric = ["Средневзвешенная цена заказчика = Σ(цена × кол-во) / Σ кол-во; отклонение от рынка = "
                    "средневзвешенная заказчика / рыночная − 1.", t("price_note", lang, cpi=cpi_month(db))]
        r.group_columns = [("customer_bin", "Заказчик"), ("enstru_name", "ТРУ"), ("unit_name", "Ед."), ("n", "N"),
                           ("median", "Медиана"), ("wavg", "Средневзв."), ("market_wavg", "Рынок"), ("vs_market", "К рынку")]
        r.groups = rows[: max(p.top_k, 15)]
        r.examples, r.columns = top_units(db, w, args, p.top_k), UNIT_COLUMNS
        r.n_examples_total = r.n_analyzed
        r.data_quality = dq_units(db, f"u.is_target AND {w}", args)
        r.data_used = data_used_block(p, codes, subjects, lang, "mart.unit_price, mart.enstru_price_stats")
    else:       # суммы по заказчикам или годам
        w, args = common_filters(p, [], "c")
        w += contract_unit_filter(p, [], args)
        args["bad"] = BAD_CONTRACT_STATUS
        by_year = len(p.bins) == 1
        key = "c.fin_year" if by_year else "c.customer_bin"
        rows = db.query(f"""
            SELECT {key} AS grp, count(*) AS n, sum(c.contract_sum_wnds) AS total,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY c.contract_sum_wnds) AS med,
                   count(DISTINCT c.supplier_bin) AS n_sup,
                   100.0 * sum(c.contract_sum_wnds) FILTER (WHERE tm.name_ru ~* 'одного источника')
                         / nullif(sum(c.contract_sum_wnds), 0) AS single_source_pct
            FROM core.contract c
            LEFT JOIN ref.status s ON s.kind = 'contract' AND s.id = c.status_id
            LEFT JOIN ref.trade_method tm ON tm.id = c.trade_method_id
            WHERE c.is_target AND NOT coalesce(c.is_deleted, false) AND coalesce(s.name_ru, '') !~* %(bad)s AND {w}
            GROUP BY 1 ORDER BY {'1' if by_year else 'total DESC'}""", args)
        for x in rows:
            x["grp"] = x["grp"] if by_year else short_name(subjects, x["grp"])
        r.n_analyzed = sum(x["n"] for x in rows)
        if rows:
            top = max(rows, key=lambda x: x["total"] or 0)
            r.summary = [L(lang,
                f"Сравнение {'по годам' if by_year else 'по заказчикам'}: {len(rows)} групп, {r.n_analyzed} договоров. "
                f"Максимальная сумма — {top['grp']}: {money(top['total'])}.",
                f"{'Жылдар' if by_year else 'Тапсырыс берушілер'} бойынша салыстыру: {len(rows)} топ, "
                f"{r.n_analyzed} келісімшарт. Ең үлкен сома — {top['grp']}: {money(top['total'])}.")]
        r.comparison = [f"Доля закупок из одного источника: от {num(min(x['single_source_pct'] or 0 for x in rows), 1)}% "
                        f"до {num(max(x['single_source_pct'] or 0 for x in rows), 1)}%"] if rows else []
        r.metric = ["Суммы договоров с НДС (contractSumWnds); доля из одного источника — по сумме."]
        r.group_columns = [("grp", "Год" if by_year else "Заказчик"), ("n", "Договоров"), ("total", "Сумма с НДС"),
                           ("med", "Медиана договора"), ("n_sup", "Поставщиков"), ("single_source_pct", "Из одного ист., %")]
        r.groups = rows[: max(p.top_k, 27)]
        r.examples, r.columns = top_contracts(db, w, args, p.top_k), CONTRACT_COLUMNS
        r.n_examples_total = r.n_analyzed
        r.data_used = data_used_block(p, [], subjects, lang, "core.contract")
    r.confidence, r.confidence_why = "high", "прямой расчёт по данным без моделей"
    r.limitations = ["Сравнение цен корректно только внутри одного кода ЕНС ТРУ и одной базовой единицы."]
    return r


def main_unit(db, w, args):
    """Самая частая базовая единица — сравнение идёт только в ней."""
    return db.one(f"""SELECT u.base_unit_code AS code, max(un.name_ru) AS name FROM mart.unit_price u
                      LEFT JOIN ref.units un ON un.code = u.base_unit_code
                      WHERE u.is_target AND {w} GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""", args)


def compare_prices_by(db, p, subjects, codes, dim):
    """Средневзвешенная цена по городам или годам."""
    lang = p.lang
    p_all = type(p)(**{**p.__dict__, "regions": []})          # города здесь группы, а не фильтр
    w, args = common_filters(p if dim == "year" else p_all, codes, "u",
                             region_col="region_code" if dim == "year" else None)
    unit = main_unit(db, w, args)
    args["unit"] = unit.get("code")
    key = "u.region_code" if dim == "region" else "u.fin_year"
    stats = {x["grp"]: x for x in db.query(f"""
        SELECT {key} AS grp, count(*) AS n, count(DISTINCT u.customer_bin) AS n_cust,
               sum(u.price_real * u.quantity_base) / sum(u.quantity_base) AS wavg,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY u.price_real) AS median
        FROM mart.unit_price u WHERE u.is_target AND {w} AND u.base_unit_code = %(unit)s GROUP BY 1""", args)}
    groups = p.regions if dim == "region" else p.period
    rows = []
    for g in groups:            # пустые группы тоже показываем
        x = stats.get(g, {"n": 0, "n_cust": 0, "wavg": None, "median": None})
        rows.append({**x, "grp": CITY_NAMES[g] if dim == "region" else g, "grp_code": g})
    r = Result(qtype="compare", n_analyzed=sum(x["n"] for x in rows))
    parts = [f"{x['grp']} — {num(x['wavg'])} тг/{unit.get('name')} (N = {x['n']})" if x["n"]
             else L(lang, f"{x['grp']} — нет закупок (N = 0)", f"{x['grp']} — сатып алу жоқ (N = 0)") for x in rows]
    with_data = [x for x in rows if x["n"]]
    diff = ""
    if len(with_data) >= 2:
        a, b = with_data[0], with_data[-1]
        diff = L(lang, f" Разница {b['grp']} к {a['grp']}: {pct((b['wavg'] / a['wavg'] - 1) * 100)}.",
                 f" {b['grp']} мен {a['grp']} айырмасы: {pct((b['wavg'] / a['wavg'] - 1) * 100)}.")
    r.summary = [L(lang, "Средневзвешенная цена: ", "Орташа өлшемді баға: ") + "; ".join(parts) + "." + diff]
    r.data_used = data_used_block(p, codes, subjects, lang, "mart.unit_price",
                                  f"единица — {unit.get('name')}; группировка — {'город' if dim == 'region' else 'год'}")
    r.comparison = [f"{x['grp']}: медиана {num(x['median'])} тг, средневзвешенная {num(x['wavg'])} тг, "
                    f"{x['n']} позиций, {x['n_cust']} заказчиков" for x in rows if x["n"]]
    r.metric = ["Средневзвешенная цена = Σ(цена × кол-во) / Σ кол-во по всем найденным кодам ЕНС ТРУ в одной базовой единице.",
                t("price_note", lang, cpi=cpi_month(db))]
    small = [x["grp"] for x in rows if x["n"] < 5]
    r.confidence = "low" if small else "medium" if min(x["n"] for x in rows) < 20 else "high"
    r.confidence_why = (f"мало позиций (< 5): {', '.join(map(str, small))}" if small
                        else f"от {min(x['n'] for x in rows)} позиций в группе")
    r.limitations = ["Только закупки 27 организаций из ТЗ; город — по КАТО места поставки позиции.",
                     "Цены приведены по ИПЦ к одному месяцу — сравнение по годам идёт в сопоставимых ценах."]
    if re.search(r"аи-?\s?9[25]", p.question.lower()):
        r.limitations.append("Марка бензина (АИ-92/95) в классификаторе ЕНС ТРУ не выделена — сравнивается весь бензин.")
    r.group_columns = [("grp", "Город" if dim == "region" else "Год"), ("n", "Позиций"), ("n_cust", "Заказчиков"),
                       ("median", "Медиана"), ("wavg", "Средневзв.")]
    r.groups = rows
    w_ex, args_ex = w + " AND u.base_unit_code = %(unit)s", dict(args)
    if dim == "region":
        w_ex += " AND u.region_code = ANY(%(regions)s)"
        args_ex["regions"] = p.regions
    r.examples, r.columns = top_units(db, w_ex, args_ex, p.top_k), UNIT_COLUMNS
    r.n_examples_total = r.n_analyzed
    r.data_quality = dq_units(db, f"u.is_target AND {w_ex}", args_ex)
    return r


# ---------- аналитика ----------

def analytics(db, p, subjects):
    lang = p.lang
    codes = resolve_items(db, p)
    if codes:
        return item_analytics(db, p, subjects, codes)
    w, args = common_filters(p, [], "c")
    w += contract_unit_filter(p, codes, args)
    args.update(bad=BAD_CONTRACT_STATUS, k=p.top_k)
    cte = f"""
        WITH c AS (
            SELECT c.*, tm.name_ru AS method FROM core.contract c
            LEFT JOIN ref.status s ON s.kind = 'contract' AND s.id = c.status_id
            LEFT JOIN ref.trade_method tm ON tm.id = c.trade_method_id
            WHERE c.is_target AND NOT coalesce(c.is_deleted, false) AND coalesce(s.name_ru, '') !~* %(bad)s AND {w}
        ) """
    years = db.query(cte + """SELECT fin_year, count(*) AS n, sum(contract_sum_wnds) AS total FROM c
                              GROUP BY 1 ORDER BY 1""", args)
    methods = db.query(cte + """SELECT coalesce(method, '—') AS method, sum(contract_sum_wnds) AS total, count(*) AS n
                                FROM c GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 5""", args)
    suppliers = db.query(cte + """SELECT supplier_bin, sum(contract_sum_wnds) AS total, count(*) AS n,
                                         (SELECT name_ru FROM core.subject sp WHERE sp.bin = c.supplier_bin) AS supplier_name
                                  FROM c GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT %(k)s""", args)
    kinds = db.query(cte + """SELECT coalesce(st.name_ru, 'не указан') AS kind, sum(contract_sum_wnds) AS total, count(*) AS n
                              FROM c LEFT JOIN ref.status st ON st.kind = 'subject_type' AND st.id = c.subject_type_id
                              GROUP BY 1 ORDER BY 2 DESC NULLS LAST""", args)
    risk = db.one(cte + """SELECT count(*) FILTER (WHERE m.risk_level = 'high') AS high,
                                  count(*) FILTER (WHERE m.risk_level = 'medium') AS medium, count(*) AS scored
                           FROM mart.ml_anomaly m JOIN mart.fair_price f USING (unit_id) JOIN c ON c.id = f.contract_id""", args)
    total = sum(x["total"] or 0 for x in years)
    n = sum(x["n"] for x in years)
    r = Result(qtype="analytics", n_analyzed=n)
    dyn = "; ".join(f"{x['fin_year']} — {money(x['total'])}" for x in years) if len(years) > 1 else ""
    r.summary = [L(lang,
        f"За {years_txt(p)} заключено {n} договоров на {money(total)} (с НДС)." + (f" Динамика по годам: {dyn}." if dyn else ""),
        f"{years_txt(p)} кезеңінде {n} келісімшарт {money(total)} сомасына жасалды (ҚҚС-пен)."
        + (f" Жылдар бойынша динамика: {dyn}." if dyn else ""))]
    r.data_used = data_used_block(p, codes, subjects, lang, "core.contract, mart.ml_anomaly, mart.fair_price")
    share = lambda x: (x["total"] or 0) / total * 100 if total else 0
    r.comparison = ["Вид предмета закупки: " + "; ".join(f"{x['kind']} — {money(x['total'])} ({share(x):.1f}%, {x['n']} дог.)"
                                                         for x in kinds)]
    r.comparison += [f"Способ закупки «{x['method']}»: {money(x['total'])} ({share(x):.1f}%), {x['n']} дог." for x in methods]
    if suppliers:
        top = suppliers[0]
        name = f" ({top['supplier_name']})" if top.get("supplier_name") else ""
        r.comparison.append(f"Крупнейший поставщик {top['supplier_bin']}{name}: {money(top['total'])} ({share(top):.1f}%)")
    r.comparison.append(f"Позиции с риском (правила + статистика + ML): высокий — {risk.get('high')}, средний — "
                        f"{risk.get('medium')} из {risk.get('scored')} оценённых")
    r.metric = ["Суммы договоров с НДС; доли — от общей суммы; риск — mart.ml_anomaly (Isolation Forest + правила + MAD/IQR)."]
    r.confidence, r.confidence_why = "high", "прямые агрегаты по данным; оценка риска — только для позиций с Fair Price"
    r.limitations = ["Договоры 2026 года — только по дату последней загрузки.",
                     "Риск оценён только для позиций, у которых есть Fair Price."]
    for x in suppliers:
        x["share"] = (x["total"] or 0) / total * 100 if total else None
    r.group_columns = [("supplier_bin", "Поставщик (БИН)"), ("supplier_name", "Название"), ("n", "Договоров"),
                       ("total", "Сумма с НДС"), ("share", "Доля, %")]
    r.groups = suppliers
    r.examples, r.columns = top_contracts(db, w, args, p.top_k), CONTRACT_COLUMNS
    r.n_examples_total = n
    return r


def item_analytics(db, p, subjects, codes):
    """Количество и сумма по годам, крупнейшие покупатели товара."""
    lang = p.lang
    w, args = common_filters(p, codes, "u", region_col="region_code")
    unit = main_unit(db, w, args)
    args.update(unit=unit.get("code"), k=p.top_k)
    base = f"FROM mart.unit_price u WHERE u.is_target AND {w} AND u.base_unit_code = %(unit)s"
    by_year = db.query(f"""SELECT u.fin_year, sum(u.quantity_base) AS qty, sum(u.price_base * u.quantity_base) AS amount,
                                  count(*) AS n {base} GROUP BY 1 ORDER BY 1""", args)
    top = db.query(f"""SELECT u.customer_bin, sum(u.quantity_base) AS qty, sum(u.price_base * u.quantity_base) AS amount,
                              count(*) AS n {base} GROUP BY 1 ORDER BY qty DESC LIMIT %(k)s""", args)
    uname = unit.get("name") or ""
    r = Result(qtype="analytics", n_analyzed=sum(x["n"] for x in by_year))
    if not by_year:
        r.summary = [L(lang, "По заданным фильтрам закупок этого ТРУ нет.", "Берілген сүзгілер бойынша бұл ТЖҚ сатып алулары жоқ.")]
        r.confidence = "high"
        r.data_used = data_used_block(p, codes, subjects, lang, "mart.unit_price")
        return r
    dyn = "; ".join(f"{x['fin_year']} — {num(x['qty'])} ({uname})" for x in by_year)
    total_qty = sum(x["qty"] for x in by_year)
    first = top[0]
    r.summary = [L(lang,
        f"Количество по годам: {dyn}; всего {num(total_qty)}. Больше всего закупил(а) "
        f"{short_name(subjects, first['customer_bin'])}: {num(first['qty'])} ({uname}) на {money(first['amount'])} без НДС.",
        f"Жылдар бойынша саны: {dyn}; барлығы {num(total_qty)}. Ең көп сатып алған — "
        f"{short_name(subjects, first['customer_bin'])}: {num(first['qty'])} ({uname}), {money(first['amount'])} ҚҚС-сыз.")]
    r.data_used = data_used_block(p, codes, subjects, lang, "mart.unit_price (позиции действующих договоров)",
                                  f"единица — {uname}")
    r.comparison = [f"{x['fin_year']}: {num(x['qty'])} ({uname}), сумма {money(x['amount'])} без НДС, {x['n']} позиций"
                    for x in by_year]
    if len(by_year) >= 2 and by_year[0]["qty"]:
        r.comparison.append(f"Изменение {by_year[0]['fin_year']} → {by_year[-1]['fin_year']}: "
                            f"{pct((by_year[-1]['qty'] / by_year[0]['qty'] - 1) * 100)}")
    r.metric = ["Количество — сумма количества позиций договоров в базовой единице; сумма — цена без НДС × количество."]
    r.confidence, r.confidence_why = "high", "прямые агрегаты по позициям договоров"
    r.limitations = ["Договоры 2026 года — только по дату последней загрузки: год неполный.",
                     "Учтены позиции действующих договоров с известными кодом ЕНС ТРУ и единицей."]
    for x in top:
        x["share"] = x["qty"] / total_qty * 100 if total_qty else None
    r.group_columns = [("customer_bin", "Заказчик (БИН)"), ("qty", f"Количество, {uname}"), ("amount", "Сумма без НДС"),
                       ("n", "Позиций"), ("share", "Доля, %")]
    r.groups = top
    w_ex = w + " AND u.base_unit_code = %(unit)s"
    r.examples, r.columns = top_units(db, w_ex, args, p.top_k), UNIT_COLUMNS
    r.n_examples_total = r.n_analyzed
    r.data_quality = dq_units(db, f"u.is_target AND {w_ex}", args)
    return r


TOOLS = {
    "anomaly": price_anomaly,
    "fairness": fairness,
    "volume": volume,
    "search": search,
    "compare": compare,
    "analytics": analytics,
}
