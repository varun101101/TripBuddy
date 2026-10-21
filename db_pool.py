"""
Connection pools for TripBuddy's Postgres database.

Hosted Postgres (Neon, Supabase, Render...) closes connections nobody is
using; Neon's free tier even suspends the whole database after about five
minutes without traffic. A connection the server has closed fails on its next
query with "the connection is closed", so a connection that has been idle for
a while gets a tiny test query before it is handed out. If the server dropped
it, all the idle connections are thrown away (they were idle just as long)
and a fresh one is opened. Connections in steady use (e.g. the group page
polling during planning) skip the test, so they stay fast.
"""

from __future__ import annotations

import atexit
import time
from typing import Any

from psycopg_pool import ConnectionPool

# Connections unused for longer than this are tested before reuse.
IDLE_CHECK_SECONDS = 30


def _mark_used(conn) -> None:
    conn._tripbuddy_last_used = time.monotonic()


def _idle_too_long(conn) -> bool:
    last_used = getattr(conn, "_tripbuddy_last_used", None)
    return last_used is None or time.monotonic() - last_used >= IDLE_CHECK_SECONDS


class TripBuddyPool(ConnectionPool):
    """A psycopg ConnectionPool that replaces connections the server has closed."""

    def getconn(self, timeout: float | None = None):
        conn = super().getconn(timeout)
        if not _idle_too_long(conn):
            return conn
        try:
            self.check_connection(conn)
            return conn
        except Exception:
            # The server dropped this connection, so it dropped the other idle
            # ones too. Discard them all (the pool opens fresh ones right
            # away) and hand out a fresh connection.
            self.putconn(conn)
            self.check()
            return super().getconn(timeout)

    def putconn(self, conn) -> None:
        _mark_used(conn)
        super().putconn(conn)


def make_pool(url: str, **kwargs: Any) -> TripBuddyPool:
    pool = TripBuddyPool(url, configure=_mark_used, open=True, **kwargs)
    # Close cleanly on exit, so stopping the server doesn't hang on the
    # pool's background threads ("couldn't stop thread ... within 5 seconds")
    atexit.register(pool.close)
    return pool
