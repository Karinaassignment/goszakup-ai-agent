-- raw -> ref, core; проверки качества; водяные знаки. Выполняется из load.py одной транзакцией.
-- core пересобирается целиком.

-- null -> [], скаляр/объект -> [значение]
CREATE OR REPLACE FUNCTION core.jarr(x jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN x IS NULL OR jsonb_typeof(x) = 'null' THEN '[]'::jsonb
        WHEN jsonb_typeof(x) = 'array' THEN x
        ELSE jsonb_build_array(x)
    END
$$;

CREATE OR REPLACE FUNCTION core.to_ts(s text) RETURNS timestamp
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN s ~ '^(19|20)\d\d-\d\d-\d\d' THEN s::timestamp END
$$;


-- Справочники: дополняются, ручные поля (ref.units.factor) не затираются

INSERT INTO ref.units (code, name_ru)
SELECT DISTINCT ON (code) code, name
FROM (
    SELECT payload->>'refUnitsCode' AS code, payload->'RefUnits'->>'nameRu' AS name
    FROM raw.plan_point
    UNION ALL
    SELECT u->'Plans'->>'refUnitsCode', u->'Plans'->'RefUnits'->>'nameRu'
    FROM raw.contract c, jsonb_array_elements(core.jarr(c.payload->'ContractUnits')) u
) s
WHERE code IS NOT NULL
ORDER BY code, name NULLS LAST
ON CONFLICT (code) DO UPDATE SET name_ru = coalesce(EXCLUDED.name_ru, ref.units.name_ru);

INSERT INTO ref.enstru (code, name_ru, name_kz)
SELECT DISTINCT ON (code) code, name, name_kz
FROM (
    SELECT payload->>'refEnstruCode' AS code, payload->'RefEnstru'->>'nameRu' AS name,
           payload->'RefEnstru'->>'nameKz' AS name_kz
    FROM raw.plan_point
    UNION ALL
    SELECT u->'Plans'->>'refEnstruCode', u->'Plans'->'RefEnstru'->>'nameRu', u->'Plans'->'RefEnstru'->>'nameKz'
    FROM raw.contract c, jsonb_array_elements(core.jarr(c.payload->'ContractUnits')) u
) s
WHERE code IS NOT NULL
ORDER BY code, name NULLS LAST, name_kz NULLS LAST
ON CONFLICT (code) DO UPDATE SET name_ru = coalesce(EXCLUDED.name_ru, ref.enstru.name_ru),
                                 name_kz = coalesce(EXCLUDED.name_kz, ref.enstru.name_kz);

-- Названий КАТО в выгрузке нет — только коды
INSERT INTO ref.kato (code)
SELECT DISTINCT code
FROM (
    SELECT k->>'refKatoCode' AS code
    FROM raw.plan_point p, jsonb_array_elements(core.jarr(p.payload->'PlansKato')) k
    UNION
    SELECT k->>'refKatoCode'
    FROM raw.contract c,
         jsonb_array_elements(core.jarr(c.payload->'ContractUnits')) u,
         jsonb_array_elements(core.jarr(u->'Plans'->'PlansKato')) k
    UNION
    SELECT jsonb_array_elements_text(core.jarr(l.payload->'plnPointKatoList'))
    FROM raw.lot l
) s
WHERE code ~ '^\d{9}$'
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.status (kind, id, name_ru)
SELECT DISTINCT ON (kind, id) kind, id, name
FROM (
    SELECT 'contract', (payload->>'refContractStatusId')::int, payload->'RefContractStatus'->>'nameRu'
    FROM raw.contract
    UNION ALL
    SELECT 'lot', (payload->>'refLotStatusId')::int, payload->'RefLotsStatus'->>'nameRu'
    FROM raw.lot
    UNION ALL
    SELECT 'buy', (payload->'TrdBuy'->>'refBuyStatusId')::int, payload->'TrdBuy'->'RefBuyStatus'->>'nameRu'
    FROM raw.lot
    UNION ALL
    SELECT 'plan', (payload->>'refPlnPointStatusId')::int, payload->'RefPlnPointStatus'->>'nameRu'
    FROM raw.plan_point
) s(kind, id, name)
WHERE id IS NOT NULL
ORDER BY kind, id, name NULLS LAST
ON CONFLICT (kind, id) DO UPDATE SET name_ru = coalesce(EXCLUDED.name_ru, ref.status.name_ru);

INSERT INTO ref.trade_method (id, name_ru)
SELECT DISTINCT ON (id) id, name
FROM (
    SELECT (payload->>'refTradeMethodsId')::int, payload->'RefTradeMethods'->>'nameRu'
    FROM raw.plan_point
    UNION ALL
    SELECT (payload->'TrdBuy'->>'refTradeMethodsId')::int, payload->'TrdBuy'->'RefTradeMethods'->>'nameRu'
    FROM raw.lot
    UNION ALL
    SELECT (payload->>'faktTradeMethodsId')::int, NULL
    FROM raw.contract
) s(id, name)
WHERE id IS NOT NULL
ORDER BY id, name NULLS LAST
ON CONFLICT (id) DO UPDATE SET name_ru = coalesce(EXCLUDED.name_ru, ref.trade_method.name_ru);

INSERT INTO ref.status (kind, id, name_ru)
SELECT DISTINCT ON (id) 'subject_type', id, name
FROM (
    SELECT (payload->>'refSubjectTypeId')::int, payload->'RefSubjectType'->>'nameRu' FROM raw.contract
    UNION ALL
    SELECT (payload->>'refSubjectTypeId')::int, payload->'RefSubjectType'->>'nameRu' FROM raw.plan_point
) s(id, name)
WHERE id IS NOT NULL
ORDER BY id, name NULLS LAST
ON CONFLICT (kind, id) DO UPDATE SET name_ru = coalesce(EXCLUDED.name_ru, ref.status.name_ru);


-- Core

TRUNCATE core.contract_unit, core.contract, core.plan_point_kato, core.plan_point,
         core.lot, core.trd_buy, core.subject;

INSERT INTO core.subject (bin, pid, name_ru, is_target)
SELECT bin, (payload->>'pid')::bigint, payload->>'nameRu', true
FROM raw.subject;

-- Поставщики с названиями из договоров, затем остальные БИН
INSERT INTO core.subject (bin, name_ru)
SELECT DISTINCT ON (payload->>'supplierBiin') payload->>'supplierBiin', payload->'Supplier'->>'nameRu'
FROM raw.contract
WHERE payload->>'supplierBiin' <> '' AND payload->'Supplier'->>'nameRu' IS NOT NULL
ORDER BY payload->>'supplierBiin', index_date DESC NULLS LAST
ON CONFLICT (bin) DO UPDATE SET name_ru = coalesce(core.subject.name_ru, EXCLUDED.name_ru);

INSERT INTO core.subject (bin)
SELECT DISTINCT b
FROM (
    SELECT payload->>'customerBin' AS b FROM raw.contract
    UNION
    SELECT payload->>'supplierBiin' FROM raw.contract
) s
WHERE b <> ''
ON CONFLICT (bin) DO NOTHING;

-- Объявления (вложены в лоты), с 2024 года
INSERT INTO core.trd_buy (id, number_anno, publish_date, fin_years, kato_codes,
                          total_sum, count_lots, trade_method_id, buy_status_id)
SELECT DISTINCT ON ((t->>'id')::bigint)
       (t->>'id')::bigint,
       t->>'numberAnno',
       core.to_ts(t->>'publishDate'),
       ARRAY(SELECT y::int FROM jsonb_array_elements_text(core.jarr(t->'finYear')) y WHERE y ~ '^\d+$'),
       ARRAY(SELECT jsonb_array_elements_text(core.jarr(t->'kato'))),
       (t->>'totalSum')::numeric,
       (t->>'countLots')::int,
       (t->>'refTradeMethodsId')::int,
       (t->>'refBuyStatusId')::int
FROM raw.lot l
CROSS JOIN LATERAL (SELECT l.payload->'TrdBuy' AS t) x
WHERE jsonb_typeof(t) = 'object'
  AND (core.to_ts(t->>'publishDate') >= '2024-01-01'
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(core.jarr(t->'finYear')) y
                  WHERE y ~ '^\d+$' AND y::int >= 2024))
ORDER BY (t->>'id')::bigint, l.index_date DESC NULLS LAST;

INSERT INTO core.lot (id, trd_buy_id, lot_number, customer_bin, name_ru, count, amount,
                      enstru_list, kato_codes, lot_status_id, trade_method_id,
                      last_update_date, index_date)
SELECT l.id,
       tb.id,
       p->>'lotNumber',
       p->>'customerBin',
       p->>'nameRu',
       (p->>'count')::numeric,
       (p->>'amount')::numeric,
       ARRAY(SELECT e::bigint FROM jsonb_array_elements_text(core.jarr(p->'enstruList')) e WHERE e ~ '^\d+$'),
       ARRAY(SELECT jsonb_array_elements_text(core.jarr(p->'plnPointKatoList'))),
       (p->>'refLotStatusId')::int,
       (p->>'refTradeMethodsId')::int,
       core.to_ts(p->>'lastUpdateDate'),
       l.index_date
FROM raw.lot l
CROSS JOIN LATERAL (SELECT l.payload AS p) x
JOIN core.trd_buy tb ON tb.id = coalesce((p->'TrdBuy'->>'id')::bigint, (p->>'trdBuyId')::bigint);

INSERT INTO core.plan_point (id, rootrecord_id, subject_bin, plan_year, month_id, name_ru,
                             enstru_code, unit_code, count, price, amount, trade_method_id,
                             status_id, subject_type_id, is_active, is_deleted, index_date)
SELECT r.id,
       (p->>'rootrecordId')::bigint,
       p->>'subjectBiin',
       (p->>'plnPointYear')::int,
       (p->>'refMonthsId')::smallint,
       p->>'nameRu',
       p->>'refEnstruCode',
       p->>'refUnitsCode',
       (p->>'count')::numeric,
       (p->>'price')::numeric,
       (p->>'amount')::numeric,
       (p->>'refTradeMethodsId')::int,
       (p->>'refPlnPointStatusId')::int,
       (p->>'refSubjectTypeId')::int,
       (p->>'isActive')::int = 1,
       (p->>'isDeleted')::int = 1,
       r.index_date
FROM raw.plan_point r
CROSS JOIN LATERAL (SELECT r.payload AS p) x;

INSERT INTO core.plan_point_kato (pln_point_id, kato_code, delivery_place)
SELECT DISTINCT ON (r.id, k->>'refKatoCode')
       r.id, k->>'refKatoCode', k->>'fullDeliveryPlaceNameRu'
FROM raw.plan_point r, jsonb_array_elements(core.jarr(r.payload->'PlansKato')) k
WHERE k->>'refKatoCode' IS NOT NULL
ORDER BY r.id, k->>'refKatoCode';

INSERT INTO core.contract (id, contract_number, sign_date, fin_year, contract_sum, contract_sum_wnds,
                           with_nds, currency, exchange_rate, customer_bin, supplier_bin, trd_buy_id,
                           status_id, trade_method_id, contract_type_id, subject_type_id, trd_buy_name,
                           fakt_sum, is_deleted, is_target, last_update_date, index_date)
SELECT r.id,
       p->>'contractNumber',
       core.to_ts(p->>'signDate'),
       (p->>'finYear')::int,
       (p->>'contractSum')::numeric,
       (p->>'contractSumWnds')::numeric,
       (p->>'withNds')::int = 1,
       p->>'refCurrencyCode',
       (p->>'exchangeRate')::numeric,
       p->>'customerBin',
       p->>'supplierBiin',
       (p->>'trdBuyId')::bigint,
       (p->>'refContractStatusId')::int,
       (p->>'faktTradeMethodsId')::int,
       (p->>'refContractTypeId')::int,
       (p->>'refSubjectTypeId')::int,
       p->>'trdBuyNameRu',
       (p->>'faktSum')::numeric,
       (p->>'deleted')::int = 1,
       r.is_target,
       core.to_ts(p->>'lastUpdateDate'),
       r.index_date
FROM raw.contract r
CROSS JOIN LATERAL (SELECT r.payload AS p) x;

INSERT INTO core.contract_unit (id, contract_id, lot_id, pln_point_id, enstru_code, unit_code, name_ru,
                                item_price, item_price_wnds, quantity, total_sum, total_sum_wnds,
                                kato_code, is_deleted)
SELECT DISTINCT ON ((u->>'id')::bigint)
       (u->>'id')::bigint,
       r.id,
       (u->>'lotId')::bigint,
       (u->>'plnPointId')::bigint,
       -- refEnstruCode приходит массивом из одного кода
       coalesce(CASE jsonb_typeof(u->'refEnstruCode')
                    WHEN 'array' THEN u->'refEnstruCode'->>0
                    ELSE u->>'refEnstruCode' END,
                u->'Plans'->>'refEnstruCode'),
       u->'Plans'->>'refUnitsCode',
       u->'Plans'->>'nameRu',
       (u->>'itemPrice')::numeric,
       (u->>'itemPriceWnds')::numeric,
       (u->>'quantity')::numeric,
       (u->>'totalSum')::numeric,
       (u->>'totalSumWnds')::numeric,
       (SELECT k->>'refKatoCode'
        FROM jsonb_array_elements(core.jarr(u->'Plans'->'PlansKato')) k
        WHERE k->>'refKatoCode' IS NOT NULL
        LIMIT 1),
       (u->>'deleted')::int = 1
FROM raw.contract r, jsonb_array_elements(core.jarr(r.payload->'ContractUnits')) u
ORDER BY (u->>'id')::bigint, r.is_target DESC;

UPDATE core.contract_unit cu
SET unit_code   = coalesce(cu.unit_code, pp.unit_code),
    enstru_code = coalesce(cu.enstru_code, pp.enstru_code),
    name_ru     = coalesce(cu.name_ru, pp.name_ru)
FROM core.plan_point pp
WHERE pp.id = cu.pln_point_id
  AND (cu.unit_code IS NULL OR cu.enstru_code IS NULL OR cu.name_ru IS NULL);

-- Часть пунктов плана API не отдаёт, тогда единица выводится:
--   customer_plan   — по плану того же заказчика, года и кода (если единица однозначна)
--   enstru_dominant — код в >= 90% записей (не меньше 5) указан в одной единице
UPDATE core.contract_unit SET unit_source = 'plan' WHERE unit_code IS NOT NULL;

UPDATE core.contract_unit cu
SET unit_code = cp.unit_code, unit_source = 'customer_plan'
FROM core.contract c,
     (SELECT subject_bin, plan_year, enstru_code, min(unit_code) AS unit_code
      FROM core.plan_point
      WHERE unit_code IS NOT NULL AND NOT coalesce(is_deleted, false)
      GROUP BY 1, 2, 3
      HAVING count(DISTINCT unit_code) = 1) cp
WHERE cu.unit_code IS NULL
  AND c.id = cu.contract_id
  AND cp.subject_bin = c.customer_bin AND cp.plan_year = c.fin_year AND cp.enstru_code = cu.enstru_code;

UPDATE core.contract_unit cu
SET unit_code = d.unit_code, unit_source = 'enstru_dominant'
FROM (SELECT DISTINCT ON (enstru_code) enstru_code, unit_code
      FROM (SELECT enstru_code, unit_code, count(*) AS n,
                   sum(count(*)) OVER (PARTITION BY enstru_code) AS total
            FROM (SELECT enstru_code, unit_code FROM core.plan_point WHERE unit_code IS NOT NULL
                  UNION ALL
                  SELECT enstru_code, unit_code FROM core.contract_unit WHERE unit_source = 'plan') o
            GROUP BY 1, 2) s
      WHERE n >= 0.9 * total AND total >= 5
      ORDER BY enstru_code, n DESC) d
WHERE cu.unit_code IS NULL AND d.enstru_code = cu.enstru_code;

-- Место поставки: пункт плана -> лот -> объявление. Пустые строки вместо КАТО сначала обнуляем.
UPDATE core.contract_unit SET kato_code = NULL WHERE kato_code !~ '^\d{9}$';

UPDATE core.contract_unit cu
SET kato_code = k.kato_code
FROM (SELECT DISTINCT ON (pln_point_id) pln_point_id, kato_code
      FROM core.plan_point_kato WHERE kato_code ~ '^\d{9}$'
      ORDER BY pln_point_id, kato_code) k
WHERE cu.kato_code IS NULL AND k.pln_point_id = cu.pln_point_id;

UPDATE core.contract_unit cu
SET kato_code = l.kato_codes[1]
FROM core.lot l
WHERE cu.kato_code IS NULL AND l.id = cu.lot_id AND l.kato_codes[1] ~ '^\d{9}$';

UPDATE core.contract_unit cu
SET kato_code = tb.kato_codes[1]
FROM core.contract c
JOIN core.trd_buy tb ON tb.id = c.trd_buy_id
WHERE cu.kato_code IS NULL AND c.id = cu.contract_id AND tb.kato_codes[1] ~ '^\d{9}$';

-- Преобладающий вид закупки по коду — для позиций без своего вида
UPDATE ref.enstru e SET subject_type_id = d.subject_type_id
FROM (SELECT DISTINCT ON (enstru_code) enstru_code, subject_type_id
      FROM (SELECT u.enstru_code, c.subject_type_id
            FROM core.contract_unit u JOIN core.contract c ON c.id = u.contract_id
            WHERE c.subject_type_id IS NOT NULL
            UNION ALL
            SELECT enstru_code, subject_type_id FROM core.plan_point WHERE subject_type_id IS NOT NULL) s
      GROUP BY enstru_code, subject_type_id
      ORDER BY enstru_code, count(*) DESC) d
WHERE e.code = d.enstru_code;


-- Проверки качества (хранится только последний запуск)

DELETE FROM etl.dq_issue;

INSERT INTO etl.dq_issue (run_id, entity, record_id, rule)
SELECT nullif(current_setting('etl.run_id', true), '')::bigint, 'contract_unit', id, rule
FROM (
    SELECT id, 'price_le_zero' AS rule FROM core.contract_unit WHERE coalesce(item_price, 0) <= 0
    UNION ALL
    SELECT id, 'qty_le_zero' FROM core.contract_unit WHERE coalesce(quantity, 0) <= 0
    UNION ALL
    SELECT id, 'total_ne_price_x_qty' FROM core.contract_unit
    WHERE abs(item_price * quantity - total_sum) > greatest(1, 0.01 * abs(total_sum))
    UNION ALL
    SELECT id, 'no_enstru' FROM core.contract_unit WHERE enstru_code IS NULL
    UNION ALL
    SELECT id, 'no_unit' FROM core.contract_unit WHERE unit_code IS NULL
    UNION ALL
    SELECT id, 'no_kato' FROM core.contract_unit WHERE kato_code IS NULL
) s;

INSERT INTO etl.dq_issue (run_id, entity, record_id, rule)
SELECT nullif(current_setting('etl.run_id', true), '')::bigint, 'contract', id, rule
FROM (
    SELECT id, 'no_sign_date' AS rule FROM core.contract WHERE sign_date IS NULL
    UNION ALL
    SELECT id, 'foreign_currency_no_rate' FROM core.contract
    WHERE currency <> 'KZT' AND exchange_rate IS NULL
) s;

INSERT INTO etl.dq_issue (run_id, entity, record_id, rule)
SELECT nullif(current_setting('etl.run_id', true), '')::bigint, 'plan_point', id, rule
FROM (
    SELECT id, 'count_le_zero' AS rule FROM core.plan_point WHERE coalesce(count, 0) <= 0
    UNION ALL
    SELECT id, 'no_enstru' FROM core.plan_point WHERE enstru_code IS NULL
) s;


-- Водяные знаки для инкрементальной загрузки

INSERT INTO etl.watermark (entity, scope, last_index_date)
SELECT 'Contract', payload->>'customerBin', max(index_date)
FROM raw.contract WHERE is_target GROUP BY 2 HAVING max(index_date) IS NOT NULL
UNION ALL
SELECT 'Plans', payload->>'subjectBiin', max(index_date)
FROM raw.plan_point GROUP BY 2 HAVING max(index_date) IS NOT NULL
UNION ALL
SELECT 'Lots', payload->>'customerBin', max(index_date)
FROM raw.lot GROUP BY 2 HAVING max(index_date) IS NOT NULL
ON CONFLICT (entity, scope) DO UPDATE
SET last_index_date = greatest(etl.watermark.last_index_date, EXCLUDED.last_index_date),
    updated_at = now();
