"""Отложенная выборка (вопросы заказчика). Прогон до доработки агента — eval/baseline_results.json.

Формат как в eval/cases.py; *_qty — количество, ±1; qtype может быть кортежем допустимых типов.
"""
from eval.cases import VALID

DIESEL = "(SELECT code FROM ref.enstru WHERE name_ru ILIKE 'Топливо дизельное%')"
GASOLINE = "(SELECT code FROM ref.enstru WHERE name_ru ILIKE 'Бензин%')"
OFFICE_PAPER = "(SELECT code FROM ref.enstru WHERE name_ru ILIKE 'Бумага для офисного%')"
NOTEBOOK = "(SELECT code FROM ref.enstru WHERE name_ru ILIKE 'Ноутбук%')"
COMPUTER = "(SELECT code FROM ref.enstru WHERE name_ru ILIKE 'Компьютер%')"
SCHOOL_FURNITURE = ("(SELECT code FROM ref.enstru WHERE name_ru ILIKE ANY "
                    "(ARRAY['Парта%', 'Мебель школ%', 'Стол учени%', 'Стул учени%']))")

ASTANA, ALMATY, SHYMKENT = "71", "75", "79"   # первые 2 цифры КАТО


def city_search_truth(codes, region, year):
    return f"""WITH c AS (SELECT c.* {VALID} AND c.fin_year = {year} AND EXISTS (
                   SELECT 1 FROM core.contract_unit u WHERE u.contract_id = c.id
                   AND u.enstru_code IN {codes} AND u.kato_code LIKE '{region}%'))
               SELECT count(*) AS n_contracts, round(sum(contract_sum_wnds), 2) AS total_money,
                      (SELECT id::text FROM c ORDER BY contract_sum_wnds DESC NULLS LAST LIMIT 1) AS top_contract_id
               FROM c"""


def city_price_by_year_truth(codes, region, unit, years):
    cols = ",\n".join(
        f"""(SELECT sum(price_real * quantity_base) / sum(quantity_base) FROM u WHERE fin_year = {y}) AS y{y}_price,
            (SELECT count(*) FROM u WHERE fin_year = {y}) AS n_{y}""" for y in years)
    return f"""WITH u AS (SELECT * FROM mart.unit_price WHERE is_target AND enstru_code IN {codes}
                          AND region_code = '{region}' AND base_unit_code = '{unit}')
               SELECT {cols}"""


def over_fair_truth(codes, year, thr):
    return f"""SELECT count(*) FILTER (WHERE deviation_pct > {thr} AND data_flag IS NULL) AS n_over,
                      count(*) FILTER (WHERE tier IS NOT NULL) AS n_priced,
                      (SELECT contract_id::text FROM mart.fair_price
                       WHERE enstru_code IN {codes} AND fin_year = {year} AND deviation_pct > {thr} AND data_flag IS NULL
                       ORDER BY (p_actual - p_fair) * quantity_base DESC LIMIT 1) AS top_contract_id
               FROM mart.fair_price WHERE enstru_code IN {codes} AND fin_year = {year}"""


CASES = [
    dict(id="H1", qtype="search",
         q="Найди все закупки дизельного топлива в Астане за 2025 год.",
         truth=city_search_truth(DIESEL, ASTANA, 2025)),
    dict(id="H2", qtype="compare",
         q="Сравни цены на бензин АИ-92 в Алматы за 2024 и 2025 годы.",
         truth=city_price_by_year_truth(GASOLINE, ALMATY, "112", [2024, 2025])),
    dict(id="H3", qtype="fairness",
         q="Рассчитай справедливую цену для лота №123456 с учетом инфляции.",
         expect_text=["не найден"]),
    dict(id="H4", qtype="fairness",
         q="Найди договоры на закупку офисной бумаги за 2025 год, где цена превышает справедливую более чем на 30%.",
         truth=over_fair_truth(OFFICE_PAPER, 2025, 30)),
    dict(id="H5", qtype=("analytics", "compare"),
         q="Какие организации закупили наибольшее количество дизельного топлива в 2025 году?",
         truth=f"""WITH g AS (SELECT customer_bin, sum(quantity_base) AS qty FROM mart.unit_price
                              WHERE is_target AND fin_year = 2025 AND enstru_code IN {DIESEL} AND base_unit_code = '112'
                              GROUP BY 1)
                   SELECT (SELECT customer_bin FROM g ORDER BY qty DESC LIMIT 1) AS top_bin,
                          (SELECT qty FROM g ORDER BY qty DESC LIMIT 1) AS top_qty"""),
    dict(id="H6", qtype="search",
         q="Покажи подробную информацию по договору №567890.",
         expect_text=["не найден"]),
    dict(id="H7", qtype="compare",
         q="Сравни стоимость закупки ноутбуков в Астане и Алматы за 2026 год.",
         truth=f"""WITH u AS (SELECT * FROM mart.unit_price WHERE is_target AND enstru_code IN {NOTEBOOK}
                              AND fin_year = 2026 AND base_unit_code = '796')
                   SELECT (SELECT sum(price_real * quantity_base) / sum(quantity_base) FROM u WHERE region_code = '{ASTANA}') AS astana_price,
                          (SELECT count(*) FROM u WHERE region_code = '{ASTANA}') AS n_astana,
                          (SELECT count(*) FROM u WHERE region_code = '{ALMATY}') AS n_almaty"""),
    dict(id="H8", qtype="search", bins=["000740001307"], years=[2024],
         q="Найди закупки организации с БИН 000740001307 за 2024 год.",
         truth=f"""WITH c AS (SELECT c.* {VALID} AND c.customer_bin = '000740001307' AND c.fin_year = 2024)
                   SELECT count(*) AS n_contracts, round(sum(contract_sum_wnds), 2) AS total_money,
                          (SELECT id::text FROM c ORDER BY contract_sum_wnds DESC NULLS LAST LIMIT 1) AS top_contract_id
                   FROM c"""),
    dict(id="H9", qtype="search", lang="kz",
         q="2025 жылы Астана қаласында дизель отынын сатып алу туралы мәліметтерді көрсет.",
         truth=city_search_truth(DIESEL, ASTANA, 2025)),
    dict(id="H10", qtype="compare", lang="kz",
         q="2024 және 2025 жылдардағы Алматы қаласындағы бензин бағаларын салыстыр.",
         truth=city_price_by_year_truth(GASOLINE, ALMATY, "112", [2024, 2025])),
    dict(id="H11", qtype="fairness", lang="kz",
         q="№987654 лот бойынша инфляцияны ескере отырып, әділ бағаны есепте.",
         expect_text=["табылмады"]),
    dict(id="H12", qtype="fairness", lang="kz",
         q="2025 жылы кеңсе қағазын сатып алу кезінде бағасы әділ бағадан 25%-дан артық болған келісімшарттарды анықта.",
         truth=over_fair_truth(OFFICE_PAPER, 2025, 25)),
    dict(id="H13", qtype=("analytics", "compare"), lang="kz",
         q="2024–2026 жылдар аралығында компьютер сатып алу көлемі қалай өзгерді?",
         truth=f"""WITH u AS (SELECT * FROM mart.unit_price WHERE is_target AND enstru_code IN {COMPUTER}
                              AND base_unit_code = '796')
                   SELECT (SELECT sum(quantity_base) FROM u WHERE fin_year = 2024) AS y2024_qty,
                          (SELECT sum(quantity_base) FROM u WHERE fin_year = 2025) AS y2025_qty,
                          (SELECT sum(quantity_base) FROM u WHERE fin_year = 2026) AS y2026_qty"""),
    dict(id="H14", qtype="search", lang="kz",
         q="№567890 келісімшарттың толық ақпаратын көрсет.",
         expect_text=["табылмады"]),
    dict(id="H15", qtype="search", lang="kz",
         q="2025 жылы Шымкент қаласында мектеп жиһазын сатып алу туралы мәліметтерді тап.",
         truth=city_search_truth(SCHOOL_FURNITURE, SHYMKENT, 2025)),
]
