"""Тип вопроса и параметры (RU / KZ) по правилам; LLM — запасной вариант для типа."""
import re
from dataclasses import dataclass, field

from . import llm
from .i18n import detect_lang

ALL_YEARS = [2024, 2025, 2026]

# volume — только «количество» + признак отклонения; «наибольшее количество» — это аналитика
QTY_WORDS = r"количеств|объ[её]м|көлем|санын|саны"
ABNORMAL_WORDS = r"завыш|нетипич|аномал|сверх|необычн|резк\w* рост|әдеттен тыс|асыр"

# порядок важен: более специфичные типы раньше
TYPE_RULES = [
    ("fairness", r"справедлив|адекватн|обоснованн?\w* цен|fair\s*price|рыночн\w* цен|әділ|негізделген"),
    ("anomaly",  r"аномал|отклонен|переплат|выброс|завыш\w* цен|дорож\w* рынк|средневзвеш|ауытқ|қымбат|орташа өлшем"),
    ("compare",  r"сравн|сопостав|\bvs\b|против|разниц|салыстыр|айырмашыл"),
    ("analytics", r"сколько|сумм[аы]|итог|структур|\bтоп\b|\btop\b|динами|анализ|статистик|доля|рейтинг|наибольш|"
                  r"наименьш|больше всего|как измен|қанша|талда|сомасы|үлесі|өзгерд|ең көп"),
    ("search",   r"найди|найти|покажи|список|какие|где|подробн|информац|тап|көрсет|тізім|мәлімет|ақпарат"),
]

# RefSubjectType: 1 товар, 2 работа, 3 услуга
SUBJECT_TYPES = {
    1: r"\b(товар|товары|товаров|товарам|тауар\w*)\b",
    2: r"\b(работа|работы|работ|работам|жұмыс\w*)\b",
    3: r"\b(услуга|услуги|услуг|услугам|қызмет\w*)\b",
}

# первые 2 цифры КАТО городов республиканского значения
CITIES = {"71": r"астан", "75": r"алмат", "79": r"шымкент"}
CITY_NAMES = {"71": "Астана", "75": "Алматы", "79": "Шымкент"}

# казахские и разговорные названия → основы слов из ЕНС ТРУ
SYNONYMS = [
    (r"кеңсе\s+қағаз\w*|офисн\w+\s+бумаг\w*", ["офисн", "бумаг"]),
    (r"қағаз\w*", ["бумаг"]),
    (r"дизель\s+отын\w*|дизельн\w+\s+топлив\w*|солярк\w*", ["дизельн", "топлив"]),
    (r"мектеп\s+жиһаз\w*|школьн\w+\s+мебел\w*", ["парта"]),
    (r"жиһаз\w*", ["мебел"]),
    (r"отын\w*", ["топлив"]),
    (r"аи-?\s?9[25]\w*|бензин\w*", ["бензин"]),
]

# корни служебных слов вопроса
STOP_ROOTS = """
найд найт покаж показ спис какие какой какая где все всех всё это эти для при про под над без через между
закуп догов контракт лот объявл план цен стоим сумм колич объ заказч поставщ организац ведомств компан
год период сколько оцен сравн сопостав проанализ анализ аномал отклон средневзв средн медиан рынк рыноч
справедл адекватн относит аналогич друг идентичн город поставк выяв нетипич завыш предыдущ данн енстру енс
тру товар услуг работ топ больш меньш выше ниже дорож дешев чем тенге процент потрат израсход затрат
итог структур динамик статистик доля рейтинг разниц против учрежд предприят способ метод вид тип
категор разрез разбивк распредел показат какова каков общ наибольш наименьш подробн информац рассчит предмет
бин бсн иин котор такой такие также этом этого этой всего когда если хотя именно которые нужно надо
алулар болға алды алған алын сатыл
учет учёт инфляц превыш более менее измен
сатып келісім баға тапсырыс беруш жеткізуш жыл бойынша тап көрсет салыстыр талда қанша сома көлем саны
ауытқ әділ нарық орташа өлшем жоғары төмен және мен үшін қымбат арзан тізім алу жылда алдың әдетт асыр
анықта жұмса шығын бағала туралы мәлімет ақпарат толық есепте ескере отырып инфляция артық болған кезінде
қаласында қаласындағы аралығында қалай өзгерд дерді деректер тауар жұмыс қызмет
""".split()


def _is_stop(word):
    return any(word.startswith(r) and (len(r) > 3 or len(word) <= len(r) + 3) for r in STOP_ROOTS)


@dataclass
class Params:
    question: str
    qtype: str = "search"
    lang: str = "ru"
    bins: list = field(default_factory=list)
    enstru: list = field(default_factory=list)
    item_terms: list = field(default_factory=list)       # основы слов наименования
    item_phrase: str = ""                                # те же слова целиком, для RAG
    synonym_terms: list = field(default_factory=list)
    item_source: str = None                              # explicit | lexical | semantic
    item_rewrite: str = None
    item_llm_selected: bool = False
    item_matches: list = field(default_factory=list)     # [(код, название, близость)]
    ignored_terms: list = field(default_factory=list)
    years: list = field(default_factory=list)
    regions: list = field(default_factory=list)
    threshold: float = 30.0
    threshold_given: bool = False
    direction: str = "up"
    lot_ref: str = None
    top_k: int = 10
    by_qty: bool = False
    subject_type: int = None
    type_source: str = "rules"

    @property
    def period(self):
        return self.years or ALL_YEARS


def _stem(word):
    return word[: max(4, len(word) - 2)] if len(word) > 5 else word


def _customer_aliases(subjects):
    """Слова, встречающиеся в названии только одной организации («Беркут», «Алатау»)."""
    owners = {}
    for bin_, name in subjects.items():
        for w in set(re.findall(r"[\w-]{5,}", (name or "").lower())):
            owners.setdefault(_stem(w), set()).add(bin_)
    return {stem: bins for stem, bins in owners.items() if len(bins) == 1}


def route(question, subjects):
    q = question.strip()
    ql = q.lower()
    p = Params(question=q, lang=detect_lang(q))

    p.by_qty = bool(re.search(QTY_WORDS, ql))
    if p.by_qty and re.search(ABNORMAL_WORDS, ql) and not re.search(TYPE_RULES[0][1], ql):
        p.qtype = "volume"
    else:
        for qtype, pattern in TYPE_RULES:
            if re.search(pattern, ql):
                p.qtype = qtype
                break
        else:
            if p.by_qty:
                p.qtype = "analytics"
            else:
                guess = llm.classify(q)
                if guess:
                    p.qtype, p.type_source = guess, "llm"

    p.bins = list(dict.fromkeys(re.findall(r"(?<!\d)\d{12}(?!\d)", q)))
    p.enstru = list(dict.fromkeys(re.findall(r"\d{6}\.\d{3}\.\d{6}", q)))

    rng = re.search(r"(20\d\d)\s*[-–—]\s*(20\d\d)", q)
    if rng:
        a, b = sorted(map(int, rng.groups()))
        p.years = [y for y in range(a, b + 1) if y in ALL_YEARS]
    else:
        p.years = sorted({int(y) for y in re.findall(r"(?<!\d)(20\d\d)(?!\d)", q) if int(y) in ALL_YEARS})

    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", q)
    if m:
        p.threshold = float(m.group(1).replace(",", "."))
        p.threshold_given = True
    if re.search(r"дешевл|ниже|заниж|арзан|төмен", ql) and not re.search(r"выше|дорож|завыш|превыш|жоғары|қымбат|артық", ql):
        p.direction = "down"

    m = re.search(r"(?:лот\w*|lot|объявлени\w*|договор\w*|хабарландыру\w*)\s*(?:№|#|номер|n\.?)?\s*([0-9][0-9A-Za-zА-Яа-яЁё\-/]*)", q, re.I)
    if m and not re.fullmatch(r"20\d\d|\d{12}", m.group(1)):   # год и БИН — не номер документа
        p.lot_ref = m.group(1)
    else:
        m = (re.search(r"[№#]\s*([0-9][0-9A-Za-zА-Яа-яЁё\-/]*)", q)
             or re.search(r"(?<![\w/])(\d{12}/\d+/\d+|\d{5,}-[A-Za-zА-Яа-яЁё]+\d*|\d{6,}-\d{1,2})(?![\w/])", q))
        if m:
            p.lot_ref = m.group(1)
    m = re.search(r"\b(?:топ|top)[-\s]?(\d{1,2})\b", ql)
    if m:
        p.top_k = max(1, min(50, int(m.group(1))))

    # фильтр по виду — только если назван ровно один
    kinds = [k for k, pattern in SUBJECT_TYPES.items() if re.search(pattern, ql)]
    if len(kinds) == 1:
        p.subject_type = kinds[0]

    # города вырезаем, чтобы не спутать с организацией или товаром
    text = ql.replace(p.lot_ref.lower(), " ") if p.lot_ref else ql
    for code, pattern in CITIES.items():
        if re.search(pattern + r"\w*", text):
            p.regions.append(code)
            text = re.sub(pattern + r"\w*", " ", text)

    aliases = _customer_aliases(subjects)
    if not p.bins:
        found = []
        for w in re.findall(r"[\w-]{4,}", text):
            found += aliases.get(_stem(w), [])
        p.bins = sorted(set(found))

    word_re = r"[a-zа-яёәғқңөұүһі][a-zа-яёәғқңөұүһі0-9-]{2,}"
    p.item_phrase = " ".join(w for w in re.findall(word_re, text)
                             if not _is_stop(w) and _stem(w) not in aliases)

    terms = []
    for pattern, stems in SYNONYMS:
        if re.search(pattern, text):
            terms += stems
            p.synonym_terms += stems
            text = re.sub(pattern, " ", text)
    words = re.findall(r"[a-zа-яёәғқңөұүһі][a-zа-яёәғқңөұүһі-]{3,}", text)
    terms += [_stem(w) for w in words if not _is_stop(w) and _stem(w) not in aliases]
    p.item_terms = list(dict.fromkeys(terms))
    return p
