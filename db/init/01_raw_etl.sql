-- raw: ответы API как есть (jsonb); etl: служебные таблицы загрузки.

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS etl;

-- Договоры 27 организаций и рыночные; при совпадении id приоритет у is_target
CREATE TABLE raw.contract (
    id          bigint PRIMARY KEY,
    payload     jsonb       NOT NULL,
    is_target   boolean     NOT NULL,
    index_date  timestamp,
    source_file text,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE raw.plan_point (
    id          bigint PRIMARY KEY,
    payload     jsonb       NOT NULL,
    index_date  timestamp,
    source_file text,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE raw.lot (
    id          bigint PRIMARY KEY,
    payload     jsonb       NOT NULL,
    index_date  timestamp,
    source_file text,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE raw.subject (
    bin         text PRIMARY KEY,
    payload     jsonb       NOT NULL,
    source_file text,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE etl.load_run (
    run_id      bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text        NOT NULL DEFAULT 'running',   -- running | ok | failed
    note        text
);

-- Неизменившиеся файлы при повторной загрузке пропускаются
CREATE TABLE etl.load_file (
    file_path     text PRIMARY KEY,
    entity        text        NOT NULL,
    file_mtime    timestamptz NOT NULL,
    rows_read     integer     NOT NULL,
    rows_upserted integer     NOT NULL,
    run_id        bigint REFERENCES etl.load_run,
    loaded_at     timestamptz NOT NULL DEFAULT now()
);

-- Последний полученный indexDate по сущности и БИН — для инкрементальной загрузки
CREATE TABLE etl.watermark (
    entity          text NOT NULL,
    scope           text NOT NULL,
    last_index_date timestamp NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity, scope)
);

CREATE TABLE etl.dq_issue (
    id         bigserial PRIMARY KEY,
    run_id     bigint REFERENCES etl.load_run,
    entity     text   NOT NULL,
    record_id  bigint,
    rule       text   NOT NULL,
    details    jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON etl.dq_issue (rule);
