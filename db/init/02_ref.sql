-- Справочники. Наполняются из вложенных объектов API; коэффициенты единиц и ИПЦ — вручную.

CREATE SCHEMA IF NOT EXISTS ref;

CREATE TABLE ref.units (
    code      text PRIMARY KEY,             -- код ОКЕИ
    name_ru   text,
    base_code text,                         -- NULL — единица сама базовая
    factor    numeric NOT NULL DEFAULT 1    -- количество в базовой = quantity * factor
);

INSERT INTO ref.units (code, name_ru, base_code, factor) VALUES
    ('166',  'Килограмм',          NULL,  1),
    ('163',  'Грамм',              '166', 0.001),
    ('168',  'Тонна',              '166', 1000),
    ('112',  'Литр (куб. дм.)',    NULL,  1),
    ('111',  'Кубический сантиметр','112', 0.001),
    ('113',  'Метр кубический',    '112', 1000),
    ('006',  'Метр',               NULL,  1),
    ('004',  'Сантиметр',          '006', 0.01),
    ('008',  'Километр',           '006', 1000),
    ('018',  'Метр погонный',      '006', 1),
    ('5156', 'Пог.м',              '006', 1);

CREATE TABLE ref.enstru (
    code    text PRIMARY KEY,
    name_ru text,
    name_kz text,
    kpved6  text GENERATED ALWAYS AS (left(code, 6)) STORED,
    subject_type_id integer                 -- преобладающий вид закупки по договорам с кодом
);

-- Векторы названий ЕНС ТРУ для семантического поиска (rag_index.py)
CREATE TABLE ref.enstru_embedding (
    code       text PRIMARY KEY,
    model      text NOT NULL,
    doc        text NOT NULL,
    doc_hash   text NOT NULL,
    emb        real[] NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Первые 2 цифры КАТО — область или город республиканского значения
CREATE TABLE ref.kato (
    code        text PRIMARY KEY,
    name_ru     text,
    region_code text GENERATED ALWAYS AS (left(code, 2)) STORED
);

CREATE TABLE ref.status (
    kind    text NOT NULL,                  -- contract | lot | buy | plan | subject_type
    id      integer NOT NULL,
    name_ru text,
    PRIMARY KEY (kind, id)
);

INSERT INTO ref.status (kind, id, name_ru) VALUES
    ('plan', 2,   'Утвержден'),
    ('plan', 3,   'Заявка'),
    ('plan', 5,   'Опубликован'),
    ('plan', 6,   'Изменен'),
    ('plan', 7,   'Отменен'),
    ('plan', 9,   'Закупка состоялась'),
    ('plan', 10,  'Закупка не состоялась'),
    ('plan', 11,  'Закупка не состоялась. Изменен'),
    ('plan', 12,  'Приостановлен'),
    ('plan', 16,  'Договор действует'),
    ('plan', 18,  'Договор не заключен'),
    ('plan', 19,  'Исполнен'),
    ('plan', 20,  'Отказ от закупки'),
    ('plan', 24,  'Передан'),
    ('plan', 29,  'Срок договора истек'),
    ('plan', 310, 'Проект договора'),
    ('plan', 320, 'Договор изменен'),
    ('plan', 340, 'Договор не исполнен. Нет платежей'),
    ('plan', 400, 'Договор не заключен, отказ от первого победителя'),
    ('plan', 410, 'Договор частично исполнен'),
    ('subject_type', 1, 'Товар'),
    ('subject_type', 2, 'Работа'),
    ('subject_type', 3, 'Услуга')
ON CONFLICT (kind, id) DO UPDATE SET name_ru = EXCLUDED.name_ru;

CREATE TABLE ref.trade_method (
    id      integer PRIMARY KEY,
    name_ru text
);

-- ИПЦ stat.gov.kz, цепной индекс (база = 100)
CREATE TABLE ref.cpi (
    period    date PRIMARY KEY,             -- первое число месяца
    cpi_index numeric NOT NULL,
    source    text
);
