"""Thin Postgres access layer -- plain SQL via psycopg2, no ORM.

Deliberately no SQLAlchemy/Alembic: this project's existing ethos (see
CONTRIBUTING.md) is to not add abstraction beyond what a task needs, and
five tables with hand-written SQL is small enough that an ORM would cost
more than it saves. schema.sql is applied idempotently (CREATE TABLE IF
NOT EXISTS) by init_schema() -- there is no migration chain to manage
because this is a young service, not yet at the point where evolving a
live schema needs a real migration tool.
"""
import atexit
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool

SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# Every cursor() used to open a fresh Postgres connection and close it
# again around ONE statement -- measured at ~6.7 transactions/second and
# 74,303 connections since the last restart, each paying a full TCP
# connect and authentication handshake.
#
# The sweeps were the worst of it by design: liveness.py's
# _close/_alive/_defer and the description backfill's _store/_defer each
# opened a connection PER POSTING, so a 20-row sweep was 20 connections.
#
# ThreadedConnectionPool rather than an ORM: the note at the top of this
# file about not adding abstraction beyond what a task needs still
# holds, and a pool leaves every existing query untouched.
#
# maxconn is deliberately generous relative to what runs here (discovery
# fans out over a thread pool, the scheduler over another) while staying
# far below Postgres's default 100 -- three processes share this ceiling.
POOL_MIN = 1
POOL_MAX = 12

# ThreadedConnectionPool.getconn() RAISES when the pool is exhausted
# rather than waiting, which is a behaviour change worth naming: before
# pooling, every caller simply got its own connection. FastAPI runs sync
# endpoints on a thread pool considerably larger than POOL_MAX, so a
# burst of concurrent requests could out-number the connections.
#
# Waiting briefly turns that into backpressure instead of an error. A
# request queueing for a few milliseconds is invisible; a request failing
# because a sweep held twelve connections is not.
POOL_WAIT_SECONDS = 5.0
POOL_RETRY_INTERVAL = 0.02

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Created lazily and under a lock.

    Lazily because importing this module must not require a reachable
    database -- the test suite imports it to skip, and a connector's
    unit tests import it transitively. Under a lock because discovery
    and the scheduler both fan out over threads, and two of them racing
    here would build two pools and leak one.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, get_dsn())
    return _pool


def _borrow(pool, wait: float = POOL_WAIT_SECONDS):
    """getconn(), but waits for a free connection instead of erroring.

    Gives up after `wait` and re-raises, because blocking forever would
    turn connection starvation into a silent hang -- far harder to
    diagnose than the error it replaces.
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            return pool.getconn()
        except psycopg2.pool.PoolError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(POOL_RETRY_INTERVAL)


def close_pool() -> None:
    """Closes every pooled connection. Registered atexit so a scheduler
    or discovery process shutting down does not leave connections for
    Postgres to time out on its own schedule."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


atexit.register(close_pool)


def get_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/internships",
    )


@contextmanager
def connect():
    """Borrows a pooled connection, commits on success, rolls back on
    error, and RETURNS it rather than closing it.

    The rollback path matters more with a pool than it did without one:
    a connection handed back mid-transaction would carry that state to
    whoever borrows it next. Returning it inside `finally` guarantees it
    goes back even when the caller raises -- a connection leaked here
    is gone until the process restarts, where before it was merely a
    closed socket.
    """
    pool = _get_pool()
    conn = _borrow(pool)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


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
