-- Витрины для агента. Обновление: SELECT mart.refresh_all(); файл можно выполнять повторно.

CREATE SCHEMA IF NOT EXISTS mart;
DROP MATERIALIZED VIEW IF EXISTS mart.unit_price CASCADE;

-- Цена за базовую единицу по каждой позиции договора
CREATE MATERIALIZED VIEW mart.unit_price AS
SELECT
    u.id                                             AS unit_id,
    c.id                                             AS contract_id,
    c.contract_number,
    c.trd_buy_id,
    u.lot_id,
    c.customer_bin,
    c.supplier_bin,
    c.is_target,
    c.sign_date,
    c.fin_year,
    date_trunc('month', c.sign_date)::date           AS sign_month,
    u.enstru_code,
    e.name_ru                                        AS enstru_name,
    coalesce(c.subject_type_id, e.subject_type_id)   AS subject_type_id,
    u.name_clean,
    u.unit_code,
    u.unit_source,
    coalesce(un.base_code, u.unit_code)              AS base_unit_code,
    u.quantity * coalesce(un.factor, 1)              AS quantity_base,
    -- без НДС, в тенге, за базовую единицу; price_real — в ценах последнего месяца ИПЦ
    u.item_price * coalesce(c.exchange_rate, 1) / coalesce(un.factor, 1)      AS price_base,
    u.item_price * coalesce(c.exchange_rate, 1) / coalesce(un.factor, 1)
        * coalesce(cpi_last.cpi_index / nullif(cpi_m.cpi_index, 0), 1)        AS price_real,
    cpi_m.period IS DISTINCT FROM date_trunc('month', c.sign_date)::date      AS cpi_carried,
    u.kato_code,
    left(u.kato_code, 2)                             AS region_code,
    -- у прямых договоров trdBuyId = 0
    CASE WHEN c.trd_buy_id > 0 THEN 'https://goszakup.gov.kz/ru/announce/index/' || c.trd_buy_id END AS anno_url
FROM core.contract_unit u
JOIN core.contract c          ON c.id = u.contract_id
LEFT JOIN ref.units un        ON un.code = u.unit_code
LEFT JOIN ref.enstru e        ON e.code = u.enstru_code
-- ИПЦ месяца подписания или последний известный до него
LEFT JOIN LATERAL (SELECT period, cpi_index FROM ref.cpi
                   WHERE period <= date_trunc('month', c.sign_date)::date
                   ORDER BY period DESC LIMIT 1) cpi_m ON true
LEFT JOIN LATERAL (SELECT cpi_index FROM ref.cpi ORDER BY period DESC LIMIT 1) cpi_last ON true
WHERE NOT coalesce(c.is_deleted, false)
  AND NOT coalesce(u.is_deleted, false)
  AND u.item_price > 0
  AND u.quantity > 0
  AND u.enstru_code IS NOT NULL
  AND u.unit_code IS NOT NULL
  AND c.sign_date IS NOT NULL
  -- «Изменен» — версия до доп. соглашения, актуальная лежит отдельным договором
  AND NOT EXISTS (
        SELECT 1 FROM ref.status s
        WHERE s.kind = 'contract' AND s.id = c.status_id
          AND s.name_ru ~* '(не заключен|расторгнут|на подписании|проект|^изменен$)');

CREATE UNIQUE INDEX ON mart.unit_price (unit_id);
CREATE INDEX ON mart.unit_price (enstru_code, base_unit_code);
CREATE INDEX ON mart.unit_price (customer_bin, fin_year);

-- Статистика цен по коду ЕНС ТРУ и единице
CREATE MATERIALIZED VIEW mart.enstru_price_stats AS
WITH base AS (
    SELECT enstru_code, base_unit_code, price_real, quantity_base, customer_bin
    FROM mart.unit_price
),
med AS (
    SELECT enstru_code, base_unit_code,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY price_real) AS median
    FROM base GROUP BY 1, 2
)
SELECT
    b.enstru_code,
    b.base_unit_code,
    count(*)                                                        AS n,
    count(DISTINCT b.customer_bin)                                  AS n_customers,
    sum(b.price_real * b.quantity_base) / sum(b.quantity_base)      AS weighted_avg,
    m.median,
    percentile_cont(0.25) WITHIN GROUP (ORDER BY b.price_real)      AS p25,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY b.price_real)      AS p75,
    percentile_cont(0.5)  WITHIN GROUP (ORDER BY abs(b.price_real - m.median)) AS mad,
    min(b.price_real)                                               AS min_price,
    max(b.price_real)                                               AS max_price
FROM base b
JOIN med m USING (enstru_code, base_unit_code)
GROUP BY b.enstru_code, b.base_unit_code, m.median;

CREATE UNIQUE INDEX ON mart.enstru_price_stats (enstru_code, base_unit_code);

-- K_region = медиана региона / медиана по стране, сглаженная к 1 при малой выборке (k = 5)
CREATE MATERIALIZED VIEW mart.region_coef AS
WITH reg AS (
    SELECT enstru_code, base_unit_code, region_code,
           count(*) AS n_region,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY price_real) AS region_median
    FROM mart.unit_price
    WHERE region_code IS NOT NULL
    GROUP BY 1, 2, 3
)
SELECT r.enstru_code, r.base_unit_code, r.region_code, r.n_region, r.region_median,
       s.median AS national_median,
       r.region_median / nullif(s.median, 0) AS coef_raw,
       (r.n_region * r.region_median / nullif(s.median, 0) + 5) / (r.n_region + 5) AS coef
FROM reg r
JOIN mart.enstru_price_stats s USING (enstru_code, base_unit_code);

CREATE UNIQUE INDEX ON mart.region_coef (enstru_code, base_unit_code, region_code);

-- Объёмы по годам: план и договоры. Из плана исключены старые версии и несостоявшаяся потребность,
-- из версий одного пункта (rootrecord_id) берётся последняя.
CREATE MATERIALIZED VIEW mart.qty_by_year AS
WITH plan_actual AS (
    SELECT DISTINCT ON (coalesce(p.rootrecord_id, p.id), p.plan_year) p.*
    FROM core.plan_point p
    WHERE NOT coalesce(p.is_deleted, false)
      AND p.enstru_code IS NOT NULL
      AND NOT EXISTS (
            SELECT 1 FROM ref.status s
            WHERE s.kind = 'plan' AND s.id = p.status_id
              AND s.name_ru ~* '(^изменен$|не состоялась|отказ|отменен|не заключен)')
    ORDER BY coalesce(p.rootrecord_id, p.id), p.plan_year, p.id DESC
),
plan AS (
    SELECT p.subject_bin AS customer_bin, p.enstru_code,
           coalesce(un.base_code, p.unit_code) AS base_unit_code,
           p.plan_year AS year,
           sum(p.count * coalesce(un.factor, 1)) AS plan_qty,
           sum(p.amount)                         AS plan_amount,
           count(*)                              AS plan_points
    FROM plan_actual p
    LEFT JOIN ref.units un ON un.code = p.unit_code
    GROUP BY 1, 2, 3, 4
),
fact AS (
    SELECT customer_bin, enstru_code, base_unit_code, fin_year AS year,
           sum(quantity_base)              AS contract_qty,
           sum(price_base * quantity_base) AS contract_amount,
           count(*)                        AS contract_units
    FROM mart.unit_price
    WHERE is_target
    GROUP BY 1, 2, 3, 4
)
SELECT coalesce(p.customer_bin, f.customer_bin)     AS customer_bin,
       coalesce(p.enstru_code, f.enstru_code)       AS enstru_code,
       coalesce(p.base_unit_code, f.base_unit_code) AS base_unit_code,
       coalesce(p.year, f.year)                     AS year,
       p.plan_qty, p.plan_amount, p.plan_points,
       f.contract_qty, f.contract_amount, f.contract_units
FROM plan p
FULL JOIN fact f
  ON  f.customer_bin = p.customer_bin AND f.enstru_code = p.enstru_code
  AND f.base_unit_code IS NOT DISTINCT FROM p.base_unit_code AND f.year = p.year;

CREATE UNIQUE INDEX ON mart.qty_by_year (customer_bin, enstru_code, base_unit_code, year);

-- Завышение количества относительно прошлых лет (по плану — он годовой).
--   growth   = план года / медиана плана прошлых лет
--   robust_z = (ln growth − медиана ln growth у заказчиков кода) / (1.4826 × MAD)
--   anomaly: growth >= 2, план >= 1 млн тг, z >= 3.5; при < 5 заказчиках кода — anomaly_no_peers
CREATE MATERIALIZED VIEW mart.qty_anomaly AS
WITH q AS (
    SELECT customer_bin, enstru_code, base_unit_code, year, plan_qty, plan_amount, plan_points,
           contract_qty, contract_amount
    FROM mart.qty_by_year
    WHERE plan_qty > 0
),
hist AS (
    SELECT cur.*, h.n_prev_years, h.prev_years, h.base_qty, h.base_amount
    FROM q cur
    CROSS JOIN LATERAL (
        SELECT count(*)                                              AS n_prev_years,
               array_agg(p.year ORDER BY p.year)                     AS prev_years,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY p.plan_qty)    AS base_qty,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY p.plan_amount) AS base_amount
        FROM q p
        WHERE p.customer_bin = cur.customer_bin AND p.enstru_code = cur.enstru_code
          AND p.base_unit_code IS NOT DISTINCT FROM cur.base_unit_code AND p.year < cur.year
    ) h
    WHERE h.n_prev_years >= 1
),
g AS (
    SELECT h.*,
           plan_qty / base_qty      AS growth,
           ln(plan_qty / base_qty)  AS log_growth
    FROM hist h
    WHERE base_qty > 0
),
peer_med AS (
    SELECT enstru_code, base_unit_code, year,
           count(*) AS n_peers,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY log_growth) AS med_log_growth
    FROM g GROUP BY 1, 2, 3
),
peer AS (
    SELECT m.*,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(g.log_growth - m.med_log_growth)) AS mad_log_growth
    FROM peer_med m
    JOIN g ON g.enstru_code = m.enstru_code
          AND g.base_unit_code IS NOT DISTINCT FROM m.base_unit_code AND g.year = m.year
    GROUP BY m.enstru_code, m.base_unit_code, m.year, m.n_peers, m.med_log_growth
),
scored AS (
    SELECT g.*,
           p.n_peers,
           exp(p.med_log_growth)  AS peer_median_growth,
           p.mad_log_growth,
           CASE WHEN p.n_peers >= 5 AND p.mad_log_growth > 0
                THEN (g.log_growth - p.med_log_growth) / (1.4826 * p.mad_log_growth) END AS robust_z
    FROM g
    JOIN peer p ON p.enstru_code = g.enstru_code
               AND p.base_unit_code IS NOT DISTINCT FROM g.base_unit_code AND p.year = g.year
)
SELECT customer_bin, enstru_code, base_unit_code, year,
       plan_qty, plan_amount, plan_points, contract_qty, contract_amount,
       prev_years, n_prev_years, base_qty, base_amount,
       growth, n_peers, peer_median_growth, mad_log_growth, robust_z,
       greatest((plan_qty - base_qty) * plan_amount / plan_qty, 0) AS excess_amount_est,
       CASE
           WHEN growth < 2 OR plan_amount < 1e6               THEN 'normal'
           WHEN n_peers < 5 OR robust_z IS NULL               THEN 'anomaly_no_peers'
           WHEN robust_z >= 3.5                               THEN 'anomaly'
           WHEN peer_median_growth >= 1.5                     THEN 'systemic_growth'
           ELSE 'high_volatility'
       END AS verdict,
       CASE
           WHEN growth < 2 OR plan_amount < 1e6               THEN NULL
           WHEN robust_z >= 3.5 AND n_prev_years >= 2         THEN 'high'
           WHEN robust_z >= 3.5 OR n_prev_years >= 2          THEN 'medium'
           ELSE 'low'
       END AS confidence
FROM scored;

CREATE UNIQUE INDEX ON mart.qty_anomaly (customer_bin, enstru_code, base_unit_code, year);
CREATE INDEX ON mart.qty_anomaly (verdict);

-- K_season = медиана месяца / медиана года; иначе 1, если данных мало или отклонение вне 5–30%
-- (больше 30% — скорее разные товары под одним кодом, чем сезонность)
CREATE MATERIALIZED VIEW mart.season_coef AS
WITH m AS (
    SELECT enstru_code, base_unit_code,
           extract(month FROM sign_date)::int AS month,
           count(*) AS n_month,
           count(DISTINCT customer_bin) AS cust_month,
           count(DISTINCT fin_year) AS years_month,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY price_real) AS month_median
    FROM mart.unit_price
    GROUP BY 1, 2, 3
)
SELECT m.enstru_code, m.base_unit_code, m.month, m.n_month, m.cust_month, m.years_month,
       m.month_median, s.n AS n_total, s.median AS year_median,
       m.month_median / nullif(s.median, 0) AS k_raw,
       CASE WHEN m.n_month >= 10 AND m.cust_month >= 3 AND m.years_month >= 2 AND s.n >= 60
                 AND abs(m.month_median / nullif(s.median, 0) - 1) BETWEEN 0.05 AND 0.30
            THEN m.month_median / s.median
            ELSE 1 END AS k_season
FROM m
JOIN mart.enstru_price_stats s USING (enstru_code, base_unit_code);

CREATE UNIQUE INDEX ON mart.season_coef (enstru_code, base_unit_code, month);

-- Fair Price. Аналоги: тот же код и единица, другой заказчик, ±24 месяца.
-- Выборка: город (4 знака КАТО) → область → страна; нужно >= 5 сделок от >= 3 заказчиков.
-- P_fair = медиана × K_region (только для страны) × K_season; D = (P / P_fair − 1) × 100%
CREATE MATERIALIZED VIEW mart.fair_price AS
WITH agg AS (
    SELECT t.*,
           a.*
    FROM mart.unit_price t
    CROSS JOIN LATERAL (
        WITH c AS MATERIALIZED (
            SELECT u.price_real, u.customer_bin, u.contract_id, u.sign_date,
                   left(u.kato_code, 4) = left(t.kato_code, 4) AS city,
                   u.region_code = t.region_code               AS reg
            FROM mart.unit_price u
            WHERE u.enstru_code = t.enstru_code
              AND u.base_unit_code = t.base_unit_code
              AND u.customer_bin <> t.customer_bin
              AND u.sign_date BETWEEN t.sign_date - interval '24 months'
                                  AND t.sign_date + interval '24 months'
        ),
        m AS (
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price_real) FILTER (WHERE city) AS med_city,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price_real) FILTER (WHERE reg)  AS med_reg,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price_real)                    AS med_nat
            FROM c
        )
        SELECT
            count(c.contract_id)         FILTER (WHERE c.city)   AS n_city,
            count(DISTINCT c.customer_bin) FILTER (WHERE c.city) AS cust_city,
            min(m.med_city)                                      AS med_city,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY c.price_real) FILTER (WHERE c.city) AS p25_city,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY c.price_real) FILTER (WHERE c.city) AS p75_city,
            percentile_cont(0.5)  WITHIN GROUP (ORDER BY abs(c.price_real - m.med_city)) FILTER (WHERE c.city) AS mad_city,
            (array_agg(c.contract_id ORDER BY c.sign_date DESC) FILTER (WHERE c.city))[1:10] AS ids_city,

            count(c.contract_id)         FILTER (WHERE c.reg)    AS n_reg,
            count(DISTINCT c.customer_bin) FILTER (WHERE c.reg)  AS cust_reg,
            min(m.med_reg)                                       AS med_reg,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY c.price_real) FILTER (WHERE c.reg) AS p25_reg,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY c.price_real) FILTER (WHERE c.reg) AS p75_reg,
            percentile_cont(0.5)  WITHIN GROUP (ORDER BY abs(c.price_real - m.med_reg)) FILTER (WHERE c.reg) AS mad_reg,
            (array_agg(c.contract_id ORDER BY c.sign_date DESC) FILTER (WHERE c.reg))[1:10] AS ids_reg,

            count(c.contract_id)           AS n_nat,
            count(DISTINCT c.customer_bin) AS cust_nat,
            min(m.med_nat)                 AS med_nat,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY c.price_real) AS p25_nat,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY c.price_real) AS p75_nat,
            percentile_cont(0.5)  WITHIN GROUP (ORDER BY abs(c.price_real - m.med_nat)) AS mad_nat,
            (array_agg(c.contract_id ORDER BY c.sign_date DESC) FILTER (WHERE c.contract_id IS NOT NULL))[1:10] AS ids_nat
        FROM m LEFT JOIN c ON true
    ) a
    WHERE t.is_target
),
tiered AS (
    SELECT g.*,
           CASE WHEN n_city >= 5 AND cust_city >= 3 THEN 'city'
                WHEN n_reg  >= 5 AND cust_reg  >= 3 THEN 'region'
                WHEN n_nat  >= 5 AND cust_nat  >= 3 THEN 'national'
           END AS tier
    FROM agg g
),
picked AS (
    SELECT t.*,
           CASE tier WHEN 'city' THEN n_city   WHEN 'region' THEN n_reg   WHEN 'national' THEN n_nat   END AS n,
           CASE tier WHEN 'city' THEN cust_city WHEN 'region' THEN cust_reg WHEN 'national' THEN cust_nat END AS n_customers,
           CASE tier WHEN 'city' THEN med_city WHEN 'region' THEN med_reg WHEN 'national' THEN med_nat END AS p_median,
           CASE tier WHEN 'city' THEN p25_city WHEN 'region' THEN p25_reg WHEN 'national' THEN p25_nat END AS p25,
           CASE tier WHEN 'city' THEN p75_city WHEN 'region' THEN p75_reg WHEN 'national' THEN p75_nat END AS p75,
           CASE tier WHEN 'city' THEN mad_city WHEN 'region' THEN mad_reg WHEN 'national' THEN mad_nat END AS mad,
           CASE tier WHEN 'city' THEN ids_city WHEN 'region' THEN ids_reg WHEN 'national' THEN ids_nat END AS analog_contract_ids,
           CASE WHEN tier = 'national' THEN coalesce(rc.coef, 1) ELSE 1 END AS k_region,
           coalesce(sc.k_season, 1) AS k_season
    FROM tiered t
    LEFT JOIN mart.region_coef rc
           ON rc.enstru_code = t.enstru_code AND rc.base_unit_code = t.base_unit_code
          AND rc.region_code = t.region_code
    LEFT JOIN mart.season_coef sc
           ON sc.enstru_code = t.enstru_code AND sc.base_unit_code = t.base_unit_code
          AND sc.month = extract(month FROM t.sign_date)::int
),
scored AS (
    SELECT p.*,
           k_region * k_season                          AS k,
           p_median * k_region * k_season               AS p_fair,
           (price_real / nullif(p_median * k_region * k_season, 0) - 1) * 100 AS deviation_pct,
           (price_real - p_median * k_region * k_season)
               / nullif(1.4826 * mad * k_region * k_season, 0)                 AS robust_z,
           -- правило Тьюки: вне [p25 − 1.5·IQR; p75 + 1.5·IQR]
           CASE WHEN price_real > (p75 + 1.5 * (p75 - p25)) * k_region * k_season THEN 'above'
                WHEN price_real < (p25 - 1.5 * (p75 - p25)) * k_region * k_season THEN 'below'
           END                                          AS iqr_flag
    FROM picked p
)
SELECT unit_id, contract_id, contract_number, trd_buy_id, lot_id, customer_bin, supplier_bin,
       sign_date, fin_year, enstru_code, enstru_name, subject_type_id, name_clean, base_unit_code, unit_source, quantity_base,
       kato_code, region_code, anno_url,
       price_real                                      AS p_actual,
       tier, n, n_customers, p_median, p25, p75, mad,
       k_region, k_season,
       p_fair,
       p_fair * price_base / nullif(price_real, 0)     AS p_fair_nominal,   -- в ценах даты договора
       deviation_pct,
       robust_z,
       iqr_flag,
       (p75 - p25) / nullif(p_median, 0)               AS rel_iqr,
       -- отклонение в 10+ раз — почти всегда ошибка единицы или цены, а не переплата
       CASE WHEN price_real / nullif(p_fair, 0) NOT BETWEEN 0.1 AND 10
            THEN 'suspect_unit_or_price_error' END     AS data_flag,
       -- вердикт: |D| > 30% и статистический выброс (|z| >= 3.5 или вне IQR)
       CASE WHEN tier IS NULL                                     THEN 'insufficient_data'
            WHEN price_real / nullif(p_fair, 0) NOT BETWEEN 0.1 AND 10 THEN 'data_error_suspect'
            WHEN deviation_pct > 30  AND (robust_z >= 3.5  OR iqr_flag = 'above') THEN 'overpriced'
            WHEN deviation_pct > 30                               THEN 'above_market_within_spread'
            WHEN deviation_pct < -30 AND (robust_z <= -3.5 OR iqr_flag = 'below') THEN 'underpriced'
            WHEN deviation_pct < -30                              THEN 'below_market_within_spread'
            ELSE 'fair' END                            AS price_verdict,
       CASE WHEN tier IS NULL THEN 'none'
            WHEN price_real / nullif(p_fair, 0) NOT BETWEEN 0.1 AND 10 THEN 'low'
            -- «Одна услуга» / «Работа»: объём в каждом договоре свой
            WHEN base_unit_code IN ('5114', '980') THEN 'low'
            WHEN tier <> 'national' AND n >= 20 AND (p75 - p25) / nullif(p_median, 0) <= 0.5 THEN 'high'
            WHEN n >= 10 AND (p75 - p25) / nullif(p_median, 0) <= 1 THEN 'medium'
            ELSE 'low' END                             AS confidence,
       analog_contract_ids,
       cpi_carried
FROM scored;

CREATE UNIQUE INDEX ON mart.fair_price (unit_id);
CREATE INDEX ON mart.fair_price (enstru_code);
CREATE INDEX ON mart.fair_price (lot_id);
CREATE INDEX ON mart.fair_price (customer_bin, fin_year);

-- Результат Isolation Forest (заполняет ml_anomaly.py)
CREATE TABLE IF NOT EXISTS mart.ml_anomaly (
    unit_id        bigint PRIMARY KEY,
    if_score       double precision NOT NULL,
    if_percentile  double precision NOT NULL,
    ml_flag        boolean NOT NULL,
    features       jsonb   NOT NULL,
    top_factors    text[]  NOT NULL,
    price_verdict  text,
    qty_verdict    text,
    risk_level     text    NOT NULL,            -- high | medium | low
    risk_reasons   text[]  NOT NULL,
    model_version  text    NOT NULL,
    scored_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ml_anomaly_risk_idx ON mart.ml_anomaly (risk_level);

CREATE OR REPLACE FUNCTION mart.refresh_all() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    REFRESH MATERIALIZED VIEW mart.unit_price;
    REFRESH MATERIALIZED VIEW mart.enstru_price_stats;
    REFRESH MATERIALIZED VIEW mart.region_coef;
    REFRESH MATERIALIZED VIEW mart.qty_by_year;
    REFRESH MATERIALIZED VIEW mart.qty_anomaly;
    REFRESH MATERIALIZED VIEW mart.season_coef;
    REFRESH MATERIALIZED VIEW mart.fair_price;
END $$;

-- Роль агента: только чтение. Пароль-заглушка; настоящий выставляет load.py из AGENT_DB_PASSWORD
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
        CREATE ROLE agent_ro LOGIN PASSWORD 'change_me';
    END IF;
END $$;
GRANT USAGE ON SCHEMA mart, ref, core TO agent_ro;
GRANT USAGE ON SCHEMA etl TO agent_ro;          -- для показа актуальности данных
GRANT SELECT ON etl.load_run TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA mart, ref, core TO agent_ro;
