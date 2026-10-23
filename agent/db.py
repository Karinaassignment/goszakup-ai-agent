"""Подключение агента к базе (роль agent_ro, только чтение)."""
import os
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "db.env")


def connect():
    password = os.getenv("AGENT_DB_PASSWORD")
    if not password:
        raise RuntimeError("Добавьте в db.env строку AGENT_DB_PASSWORD=<пароль роли agent_ro>")
    conn = psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "5433")),
        dbname=os.getenv("POSTGRES_DB", "goszakup"),
        user=os.getenv("AGENT_DB_USER", "agent_ro"),
        password=password,
        options="-c default_transaction_read_only=on -c statement_timeout=60000",
    )
    conn.autocommit = True
    return conn


def _plain(v):
    return float(v) if isinstance(v, Decimal) else v


class DB:
    """Выполняет запросы и сохраняет их текст для раздела «Как воспроизвести»."""

    def __init__(self, conn=None):
        self.conn = conn or connect()
        self.log = []

    def query(self, sql, params=None):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            self.log.append(cur.mogrify(sql, params).decode())
            cur.execute(sql, params)
            return [{k: _plain(v) for k, v in row.items()} for row in cur.fetchall()]

    def one(self, sql, params=None):
        rows = self.query(sql, params)
        return rows[0] if rows else {}

    def close(self):
        self.conn.close()
