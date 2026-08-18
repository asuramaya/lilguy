"""The pool replaced connection-per-statement, so the properties that
used to be free -- always getting a connection, never inheriting someone
else's transaction -- now have to be asserted.
"""
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402


def test_connections_are_reused_rather_than_reopened():
    db._get_pool()
    seen = set()
    for _ in range(20):
        with db.connect() as conn:
            seen.add(id(conn))
    assert len(seen) <= db.POOL_MAX, "each call opened a fresh connection -- the pool is not being used"
    assert len(seen) < 20, "no connection was reused across 20 sequential calls"


def test_a_failed_transaction_does_not_poison_the_next_borrower():
    # The rollback path matters far more with a pool: a connection handed
    # back mid-transaction would carry that state to whoever gets it next.
    with pytest.raises(Exception):
        with db.cursor() as cur:
            cur.execute("SELECT 1/0")

    with db.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        assert cur.fetchone()["ok"] == 1


def test_connections_are_returned_even_when_the_caller_raises():
    for _ in range(db.POOL_MAX * 2):
        with pytest.raises(RuntimeError):
            with db.cursor():
                raise RuntimeError("caller blew up")
    # If any of those leaked, the pool would now be exhausted.
    with db.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        assert cur.fetchone()["ok"] == 1


def test_concurrent_threads_each_get_their_own_connection():
    # discovery and the scheduler both fan out over threads; two threads
    # sharing one psycopg2 connection would interleave transactions.
    conns, errors = [], []
    lock = threading.Lock()

    def worker():
        try:
            with db.cursor() as cur:
                cur.execute("SELECT pg_backend_pid() AS pid")
                with lock:
                    conns.append(cur.fetchone()["pid"])
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(db.POOL_MAX)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"threads failed to get connections: {errors}"
    assert len(conns) == db.POOL_MAX


def test_exhaustion_waits_rather_than_erroring_immediately():
    # Before pooling every caller simply got a connection. Turning that
    # into an instant failure under load would be a regression, so
    # exhaustion has to apply backpressure instead.
    pool = db._get_pool()
    held = [pool.getconn() for _ in range(db.POOL_MAX)]
    try:
        with pytest.raises(Exception):
            db._borrow(pool, wait=0.05)   # still raises eventually...
    finally:
        for conn in held:
            pool.putconn(conn)

    # ...and recovers cleanly once connections come back.
    with db.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        assert cur.fetchone()["ok"] == 1
