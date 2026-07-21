from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from assistant import config

_SCHEMA = Path(__file__).parent / "schema.sql"


def init_schema() -> None:
    # Plain connection (no vector adapter): the extension may not exist yet.
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(_SCHEMA.read_text())


def get_connection() -> psycopg.Connection:
    conn = psycopg.connect(config.DATABASE_URL, autocommit=True)
    register_vector(conn)
    return conn
