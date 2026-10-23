-- Векторы названий ЕНС ТРУ для существующей базы (заполняет rag_index.py).
CREATE TABLE IF NOT EXISTS ref.enstru_embedding (
    code       text PRIMARY KEY,
    model      text NOT NULL,
    doc        text NOT NULL,
    doc_hash   text NOT NULL,
    emb        real[] NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT ON ref.enstru_embedding TO agent_ro;
