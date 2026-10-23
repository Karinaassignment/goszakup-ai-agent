-- Вид предмета закупки и поставщики для существующей базы.
-- После миграции: функции очистки из 03_core.sql и python load.py.

ALTER TABLE core.contract   ADD COLUMN IF NOT EXISTS subject_type_id integer;
ALTER TABLE core.contract   ADD COLUMN IF NOT EXISTS trd_buy_name    text;
ALTER TABLE core.contract   ADD COLUMN IF NOT EXISTS fakt_sum        numeric(18,2);
ALTER TABLE core.plan_point ADD COLUMN IF NOT EXISTS subject_type_id integer;
ALTER TABLE ref.enstru      ADD COLUMN IF NOT EXISTS subject_type_id integer;

INSERT INTO ref.status (kind, id, name_ru) VALUES
    ('subject_type', 1, 'Товар'), ('subject_type', 2, 'Работа'), ('subject_type', 3, 'Услуга')
ON CONFLICT (kind, id) DO UPDATE SET name_ru = EXCLUDED.name_ru;
