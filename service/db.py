"""Thin Postgres access layer -- plain SQL via psycopg2, no ORM.

Deliberately no SQLAlchemy/Alembic: this project's existing ethos (see
CONTRIBUTING.md) is to not add abstraction beyond what a task needs, and
five tables with hand-written SQL is small enough that an ORM would cost
more than it saves. schema.sql is applied idempotently (CREATE TABLE IF
NOT EXISTS) by init_schema() -- there is no migration chain to manage
because this is a young service, not yet at the point where evolving a
live schema needs a real migration tool.
"""
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras

SCHEMA_FILE = Path(__file__).parent / "schema.sql"


def get_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/internships",
    )


@contextmanager
def connect():
    conn = psycopg2.connect(get_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def cursor():
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur


def init_schema() -> None:
    sql = SCHEMA_FILE.read_text()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
