"""Основной набор вопросов. Эталон — независимый SQL; каждая колонка — проверяемый факт.

Суффикс колонки задаёт сравнение: *_id/_bin/_number/_date — дословно, n_* — точно,
*_money ±1 тг, *_price ±0.01, *_pct ±0.1. expect_text — обязательные фразы ответа.
"""

VALID = """
    FROM core.contract c
    LEFT JOIN ref.status s ON s.kind = 'contract' AND s.id = c.status_id
    WHERE c.is_target AND NOT coalesce(c.is_deleted, false)
      AND coalesce(s.name_ru, '') !~* '(не заключен|^изменен$|на подписании|проект)'"""

# средневзвешенная считается заново из позиций, а не берётся из mart.enstru_price_stats
WAVG = """
    s AS (SELECT enstru_code, base_unit_code,
                 sum(price_real * quantity_base) / sum(quantity_base) AS wavg
          FROM mart.unit_price GROUP BY 1, 2
          HAVING count(*) >= 5 AND count(DISTINCT customer_bin) >= 3),
    b AS (SELECT u.*, s.wavg, u.price_real / s.wavg AS ratio
          FROM mart.unit_price u JOIN s USING (enstru_code, base_unit_code)
          WHERE u.is_target {filter})"""

DIESEL = "(SELECT code FROM ref.enstru WHERE name_ru ILIKE 'Топливо дизельное%')"


def anomaly_truth(filter_sql="", up=True, thr=0.30):
    flag = f"ratio > {1 + thr} AND ratio <= 10" if up else f"ratio < {1 - thr} AND ratio >= 0.1"
    top = (f"(SELECT contract_id FROM b WHERE {flag} ORDER BY (price_real - wavg) * quantity_base DESC LIMIT 1)::text AS top_contract_id,"
           if up else "")
    excess = f", round(sum((price_real - wavg) * quantity_base) FILTER (WHERE {flag})) AS excess_money" if up else ""
    return f"""WITH {WAVG.format(filter=filter_sql)}
        SELECT {top} count(*) AS n_analyzed, count(*) FILTER (WHERE {flag}) AS n_flagged {excess} FROM b"""


def fair_truth(cond):
    return f"""SELECT f.contract_id::text AS contract_id, u.price_base AS actual_price,
                      f.p_fair_nominal AS fair_price, round(f.deviation_pct::numeric, 1) AS deviation_pct,
                      f.n AS n_comparables, f.n_customers AS n_customers
               FROM mart.fair_price f JOIN mart.unit_price u USING (unit_id)
               WHERE {cond}"""


def volume_truth(cond="year IN (2025, 2026)"):
    return f"""SELECT count(*) AS n_total,
                      count(*) FILTER (WHERE verdict = 'anomaly') AS n_anomaly,
                      count(*) FILTER (WHERE verdict = 'anomaly_no_peers') AS n_candidates,
                      round(sum(excess_amount_est) FILTER (WHERE verdict = 'anomaly')) AS excess_money
               FROM mart.qty_anomaly WHERE {cond}"""


def search_truth(cond):
    return f"""WITH c AS (SELECT c.* {VALID} AND {cond})
               SELECT count(*) AS n_contracts, round(sum(contract_sum_wnds), 2) AS total_money,
                      (SELECT id::text FROM c ORDER BY contract_sum_wnds DESC NULLS LAST LIMIT 1) AS top_contract_id,
                      (SELECT to_char(sign_date, 'DD.MM.YYYY') FROM c ORDER BY contract_sum_wnds DESC NULLS LAST LIMIT 1) AS top_date,
                      (SELECT supplier_bin FROM c ORDER BY contract_sum_wnds DESC NULLS LAST LIMIT 1) AS top_supplier_bin
               FROM c"""


def compare_prices_truth(code, unit):
    return f"""WITH g AS (
                   SELECT customer_bin, sum(price_real * quantity_base) / sum(quantity_base) AS wavg
                   FROM mart.unit_price
                   WHERE is_target AND enstru_code = '{code}' AND base_unit_code = '{unit}'
                   GROUP BY 1)
               SELECT (SELECT customer_bin FROM g ORDER BY wavg DESC LIMIT 1) AS high_bin,
                      (SELECT wavg FROM g ORDER BY wavg DESC LIMIT 1) AS high_price,
                      (SELECT customer_bin FROM g ORDER BY wavg LIMIT 1) AS low_bin,
                      (SELECT wavg FROM g ORDER BY wavg LIMIT 1) AS low_price"""


CASES = [
    # аномалии цены
    dict(id="A1", qtype="anomaly",
         q="Найди закупки с отклонением цены > 30% от средневзвешенной по данному ЕНСТРУ",
         truth=anomaly_truth()),
    dict(id="A2", qtype="anomaly", enstru=["192026.510.000000"],
         q="Найди закупки по коду 192026.510.000000 с отклонением цены > 50% от средневзвешенной",
         truth=anomaly_truth("AND u.enstru_code = '192026.510.000000'", thr=0.5)),
    dict(id="A3", qtype="anomaly",
         q="Найди закупки, цена которых ниже средневзвешенной по ЕНС ТРУ более чем на 40%",
         truth=anomaly_truth(up=False, thr=0.4)),
    dict(id="A4", qtype="anomaly", bins=["990740002243"], years=[2025],
         q="Найди закупки 990740002243 в 2025 году с отклонением цены > 30% от средневзвешенной",
         truth=anomaly_truth("AND u.customer_bin = '990740002243' AND u.fin_year = 2025")),
    dict(id="A5", qtype="anomaly",
         q="Найди закупки дизельного топлива с отклонением цены > 30% от средневзвешенной",
         truth=anomaly_truth(f"AND u.enstru_code IN {DIESEL}")),
    dict(id="A6", qtype="anomaly", lang="kz",
         q="Дизель отыны бағасы орташа өлшемді бағадан 30% жоғары сатып алуларды тап",
         truth=anomaly_truth(f"AND u.enstru_code IN {DIESEL}")),

    # справедливость цены
    dict(id="F1", qtype="fairness", lot="11-2024/ООФ",
         q="Оцени адекватность цены договора 11-2024/ООФ относительно аналогичных контрактов других ведомств в идентичном городе поставки",
         truth=fair_truth("f.contract_number = '11-2024/ООФ'")),
    dict(id="F2", qtype="fairness", lot="000740001307/250052/01",
         q="Оцени адекватность цены договора 000740001307/250052/01 относительно аналогичных контрактов других ведомств",
         truth=fair_truth("f.contract_number = '000740001307/250052/01'")),
    dict(id="F3", qtype="fairness", lot="71742654-КПКО1",
         q="Оцени справедливость цены лота 71742654-КПКО1",
         truth=fair_truth("f.lot_id = (SELECT id FROM core.lot WHERE lot_number = '71742654-КПКО1')")),
    dict(id="F4", qtype="fairness", lang="kz", lot="000740001307/250052/01",
         q="000740001307/250052/01 келісімшарт бағасының әділдігін бағала",
         truth=fair_truth("f.contract_number = '000740001307/250052/01'")),
    dict(id="F5", qtype="fairness", lot="99999999-ЗЦП1",
         q="Оцени адекватность цены лота 99999999-ЗЦП1",
         expect_text=["не найден"]),
    dict(id="F6", qtype="fairness", lot="81064468-ЗЦП4",
         q="Оцени адекватность цены лота 81064468-ЗЦП4",
         expect_text=["нет заключённого договора"],
         truth="SELECT customer_bin FROM core.lot WHERE lot_number = '81064468-ЗЦП4'"),

    # объёмы
    dict(id="V1", qtype="volume",
         q="Выяви нетипичное завышение количества ТРУ по сравнению с предыдущими годами",
         truth=volume_truth()),
    dict(id="V2", qtype="volume", bins=["990740002243"], years=[2025],
         q="Выяви нетипичное завышение количества закупок у 990740002243 в 2025 году",
         truth=volume_truth("year = 2025 AND customer_bin = '990740002243'")),
    dict(id="V3", qtype="volume", lang="kz",
         q="Алдыңғы жылдармен салыстырғанда ТЖҚ санының әдеттен тыс асырылуын анықта",
         truth=volume_truth()),

    # поиск
    dict(id="S1", qtype="search", bins=["000740001307"], years=[2025],
         q="Найди договоры заказчика 000740001307 за 2025 год",
         truth=search_truth("c.customer_bin = '000740001307' AND c.fin_year = 2025")),
    dict(id="S2", qtype="search", enstru=["192026.510.000000"], years=[2024],
         q="Покажи договоры по коду 192026.510.000000 в 2024 году",
         truth=search_truth("c.fin_year = 2024 AND EXISTS (SELECT 1 FROM core.contract_unit u "
                            "WHERE u.contract_id = c.id AND u.enstru_code = '192026.510.000000')")),
    dict(id="S3", qtype="search", lang="kz", bins=["990740002243"], years=[2026],
         q="990740002243 тапсырыс берушінің 2026 жылғы келісімшарттарын көрсет",
         truth=search_truth("c.customer_bin = '990740002243' AND c.fin_year = 2026")),
    dict(id="S4", qtype="search", bins=["971040001050"],
         q="Найди договоры на дизельное топливо у 971040001050",
         truth=search_truth(f"c.customer_bin = '971040001050' AND EXISTS (SELECT 1 FROM core.contract_unit u "
                            f"WHERE u.contract_id = c.id AND u.enstru_code IN {DIESEL})")),

    # аналитика
    dict(id="N1", qtype="analytics", bins=["000740001307"], years=[2024, 2025],
         q="Сколько потратил Беркут в 2024-2025 годах",
         truth=f"""SELECT count(*) AS n_contracts,
                          round(sum(contract_sum_wnds) FILTER (WHERE fin_year = 2024), 2) AS y2024_money,
                          round(sum(contract_sum_wnds) FILTER (WHERE fin_year = 2025), 2) AS y2025_money,
                          round(sum(contract_sum_wnds), 2) AS total_money
                   {VALID} AND c.customer_bin = '000740001307' AND c.fin_year IN (2024, 2025)"""),
    dict(id="N2", qtype="analytics", bins=["960440000220"], years=[2025],
         q="Какова сумма договоров 960440000220 за 2025 год",
         truth=f"""WITH c AS (SELECT c.* {VALID} AND c.customer_bin = '960440000220' AND c.fin_year = 2025)
                   SELECT count(*) AS n_contracts, round(sum(contract_sum_wnds), 2) AS total_money,
                          (SELECT supplier_bin FROM c GROUP BY 1 ORDER BY sum(contract_sum_wnds) DESC NULLS LAST LIMIT 1) AS top_supplier_bin,
                          (SELECT round(sum(contract_sum_wnds), 2) FROM c GROUP BY supplier_bin
                           ORDER BY sum(contract_sum_wnds) DESC NULLS LAST LIMIT 1) AS top_supplier_money
                   FROM c"""),
    dict(id="N3", qtype="analytics", lang="kz", bins=["000740001307"], years=[2025],
         q="Беркут 2025 жылы қанша жұмсады",
         truth=f"""SELECT count(*) AS n_contracts, round(sum(contract_sum_wnds), 2) AS total_money
                   {VALID} AND c.customer_bin = '000740001307' AND c.fin_year = 2025"""),
    dict(id="N4", qtype="analytics", bins=["020440003656"], years=[2025],
         q="Структура закупок 020440003656 по способам закупки в 2025 году",
         truth=f"""SELECT round(sum(c.contract_sum_wnds), 2) AS top_method_money, count(*) AS n_top_method
                   {VALID} AND c.customer_bin = '020440003656' AND c.fin_year = 2025
                   GROUP BY c.trade_method_id ORDER BY sum(c.contract_sum_wnds) DESC NULLS LAST LIMIT 1"""),

    # вид предмета закупки
    dict(id="T1", qtype="analytics", bins=["000740001307"], years=[2025],
         q="Сколько потратил Беркут на услуги в 2025 году",
         truth=f"""SELECT count(*) AS n_contracts, round(sum(contract_sum_wnds), 2) AS total_money
                   {VALID} AND c.customer_bin = '000740001307' AND c.fin_year = 2025 AND c.subject_type_id = 3"""),
    dict(id="T2", qtype="search", lang="kz", bins=["990740002243"], years=[2026],
         q="990740002243 тапсырыс берушінің 2026 жылғы жұмыс келісімшарттарын көрсет",
         truth=search_truth("c.customer_bin = '990740002243' AND c.fin_year = 2026 AND c.subject_type_id = 2")),
    dict(id="T3", qtype="analytics", bins=["020440003656"], years=[2025],
         q="Структура закупок 020440003656 по видам предмета закупки в 2025 году",
         truth=f"""WITH g AS (SELECT c.subject_type_id, sum(c.contract_sum_wnds) AS total, count(*) AS n
                             {VALID} AND c.customer_bin = '020440003656' AND c.fin_year = 2025 GROUP BY 1)
                   SELECT (SELECT round(total, 2) FROM g WHERE subject_type_id = 1) AS goods_money,
                          (SELECT round(total, 2) FROM g WHERE subject_type_id = 3) AS services_money,
                          (SELECT n FROM g WHERE subject_type_id = 1) AS n_goods"""),

    # сравнение
    dict(id="C1", qtype="compare", years=[2025],
         q="Сравни заказчиков по сумме договоров за 2025",
         truth=f"""WITH g AS (SELECT c.customer_bin, sum(c.contract_sum_wnds) AS total, count(*) AS n
                             {VALID} AND c.fin_year = 2025 GROUP BY 1)
                   SELECT (SELECT customer_bin FROM g ORDER BY total DESC LIMIT 1) AS top_bin,
                          (SELECT round(total, 2) FROM g ORDER BY total DESC LIMIT 1) AS top_money,
                          (SELECT sum(n) FROM g) AS n_contracts"""),
    dict(id="C2", qtype="compare", enstru=["192026.510.000000"],
         q="Сравни цены по коду 192026.510.000000 между заказчиками",
         truth=compare_prices_truth("192026.510.000000", "112")),
    dict(id="C3", qtype="compare", bins=["000740001307"],
         q="Сравни закупки 000740001307 по годам",
         truth=f"""SELECT round(sum(contract_sum_wnds) FILTER (WHERE fin_year = 2024), 2) AS y2024_money,
                          round(sum(contract_sum_wnds) FILTER (WHERE fin_year = 2025), 2) AS y2025_money,
                          round(sum(contract_sum_wnds) FILTER (WHERE fin_year = 2026), 2) AS y2026_money,
                          count(*) AS n_contracts
                   {VALID} AND c.customer_bin = '000740001307'"""),
    dict(id="C4", qtype="compare", lang="kz", enstru=["192026.510.000000"],
         q="192026.510.000000 коды бойынша тапсырыс берушілердің бағаларын салыстыр",
         truth=compare_prices_truth("192026.510.000000", "112")),
]
