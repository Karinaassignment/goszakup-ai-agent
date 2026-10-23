-- Нормализованные таблицы. Связи со справочниками без FK: API отдаёт коды, которых в них может не быть.

CREATE SCHEMA IF NOT EXISTS core;

-- Латинские буквы-двойники в русских словах и наоборот: слово приводится к преобладающему алфавиту
CREATE OR REPLACE FUNCTION core.fix_homoglyphs(s text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    w   text;
    res text := '';
BEGIN
    IF s IS NULL OR s !~ '[A-Za-z]' OR s !~ '[А-Яа-яЁё]' THEN
        RETURN s;
    END IF;
    FOR w IN SELECT m[1] FROM regexp_matches(s, '([A-Za-zА-Яа-яЁё]+|[^A-Za-zА-Яа-яЁё]+)', 'g') AS m LOOP
        IF w ~ '[А-Яа-яЁё]' AND w ~ '[A-Za-z]' THEN
            IF length(regexp_replace(w, '[^А-Яа-яЁё]', '', 'g')) >= length(regexp_replace(w, '[^A-Za-z]', '', 'g')) THEN
                w := translate(w, 'AaBCcEeHKMOoPpTXxy', 'АаВСсЕеНКМОоРрТХху');
            ELSE
                w := translate(w, 'АаВСсЕеНКМОоРрТХху', 'AaBCcEeHKMOoPpTXxy');
            END IF;
        END IF;
        res := res || w;
    END LOOP;
    RETURN res;
END $$;

-- Очистка названий: глагол в начале («Приобретение …»), год и срок поставки, подразделение в скобках,
-- «и т.д.», лишняя пунктуация и регистр. Год убирается только рядом с «на/в/за» или «год/г.».
CREATE OR REPLACE FUNCTION core.clean_name(s text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT nullif(btrim(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
           regexp_replace(regexp_replace(regexp_replace(regexp_replace(
               lower(core.fix_homoglyphs(s)),
               '\((служб|отдел|управлени|департамент|филиал|цех|для нужд|по заявке)[^)]*\)', ' ', 'g'),
               '\m(и\s*\.?\s*т\s*\.?\s*д|и\s*\.?\s*т\s*\.?\s*п|и\s+др|и\s+прочее)\.?', ' ', 'g'),
               '^\s*(закуп|приобретени|покупк|поставк|оказани|выполнени)\w*\s+', '', 'g'),
               'поставк\w*\s+в\s+течени\w+\s+(всего\s+)?года', ' ', 'g'),
               '\m((на|в|за)\s+20(1[5-9]|2\d|3[0-5])(\s*[-–]\s*20\d\d)?(\s*(годы|годах|году|года|год|гг|г))?'
               '|20(1[5-9]|2\d|3[0-5])(\s*[-–]\s*20\d\d)?\s*(годы|годах|году|года|год|гг|г))\M\.?', ' ', 'g'),
               '[«»"''`“”№#*_|\\]+', ' ', 'g'),
               '\s*,\s*', ', ', 'g'),
               '[\s,]+([)\]])', '\1', 'g'),
               '[\s.,;:\-]+$', '', 'g'),
               '\s+', ' ', 'g')), '')
$$;

-- Заказчики и поставщики
CREATE TABLE core.subject (
    bin       text PRIMARY KEY,
    pid       bigint,
    name_ru   text,
    is_target boolean NOT NULL DEFAULT false      -- входит в список 27 bins из ТЗ
);

-- Объявления (TrdBuy, приходят вложенными в Lots)
CREATE TABLE core.trd_buy (
    id               bigint PRIMARY KEY,
    number_anno      text,
    publish_date     timestamp,
    fin_years        integer[],
    kato_codes       text[],
    total_sum        numeric(18,2),
    count_lots       integer,
    trade_method_id  integer,
    buy_status_id    integer
);

-- Лоты
CREATE TABLE core.lot (
    id                bigint PRIMARY KEY,
    trd_buy_id        bigint REFERENCES core.trd_buy,
    lot_number        text,
    customer_bin      text,
    name_ru           text,
    name_clean        text GENERATED ALWAYS AS (core.clean_name(name_ru)) STORED,
    count             numeric,
    amount            numeric(18,2),
    enstru_list       bigint[],
    kato_codes        text[],
    lot_status_id     integer,
    trade_method_id   integer,
    last_update_date  timestamp,
    index_date        timestamp
);
CREATE INDEX ON core.lot (customer_bin);
CREATE INDEX ON core.lot (trd_buy_id);

-- Пункты годового плана
CREATE TABLE core.plan_point (
    id               bigint PRIMARY KEY,
    rootrecord_id    bigint,
    subject_bin      text,
    plan_year        integer,
    month_id         smallint,
    name_ru          text,
    name_clean       text GENERATED ALWAYS AS (core.clean_name(name_ru)) STORED,
    enstru_code      text,
    unit_code        text,
    count            numeric,
    price            numeric(18,2),
    amount           numeric(18,2),
    trade_method_id  integer,
    status_id        integer,
    subject_type_id  integer,                  -- вид предмета закупки: 1 Товар, 2 Работа, 3 Услуга
    is_active        boolean,
    is_deleted       boolean,
    index_date       timestamp
);
CREATE INDEX ON core.plan_point (subject_bin, plan_year);
CREATE INDEX ON core.plan_point (enstru_code);

-- Места поставки пункта плана (PlansKato — массив)
CREATE TABLE core.plan_point_kato (
    pln_point_id    bigint REFERENCES core.plan_point ON DELETE CASCADE,
    kato_code       text,
    delivery_place  text,
    PRIMARY KEY (pln_point_id, kato_code)
);

-- Договоры (свои + рыночные)
CREATE TABLE core.contract (
    id                bigint PRIMARY KEY,
    contract_number   text,
    sign_date         timestamp,
    fin_year          integer,
    contract_sum      numeric(18,2),
    contract_sum_wnds numeric(18,2),
    with_nds          boolean,
    currency          text,
    exchange_rate     numeric,
    customer_bin      text,
    supplier_bin      text,
    trd_buy_id        bigint,                  -- без FK: у рыночных договоров объявлений в базе нет
    status_id         integer,
    trade_method_id   integer,
    contract_type_id  integer,
    subject_type_id   integer,                 -- вид предмета закупки: 1 Товар, 2 Работа, 3 Услуга
    trd_buy_name      text,                    -- название объявления
    fakt_sum          numeric(18,2),           -- фактически исполненная сумма
    is_deleted        boolean,
    is_target         boolean NOT NULL,
    last_update_date  timestamp,
    index_date        timestamp
);
CREATE INDEX ON core.contract (customer_bin, fin_year);
CREATE INDEX ON core.contract (supplier_bin);
CREATE INDEX ON core.contract (trd_buy_id);

-- Позиции договора — главная таблица для анализа цен
CREATE TABLE core.contract_unit (
    id               bigint PRIMARY KEY,
    contract_id      bigint NOT NULL REFERENCES core.contract ON DELETE CASCADE,
    lot_id           bigint,
    pln_point_id     bigint,
    enstru_code      text,                     -- refEnstruCode[0], иначе Plans.refEnstruCode
    unit_code        text,                     -- Plans.refUnitsCode
    unit_source      text,                     -- plan | customer_plan | enstru_dominant (см. transform.sql)
    name_ru          text,                     -- Plans.nameRu
    name_clean       text GENERATED ALWAYS AS (core.clean_name(name_ru)) STORED,
    item_price       numeric(18,4),            -- цена за единицу без НДС
    item_price_wnds  numeric(18,4),            -- с НДС
    quantity         numeric,
    total_sum        numeric(18,2),
    total_sum_wnds   numeric(18,2),
    kato_code        text,                     -- Plans.PlansKato[0], иначе KATO лота/объявления
    is_deleted       boolean
);
CREATE INDEX ON core.contract_unit (contract_id);
CREATE INDEX ON core.contract_unit (enstru_code);
CREATE INDEX ON core.contract_unit (lot_id);
