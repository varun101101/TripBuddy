"""
The app must survive the database closing its connections.

Hosted Postgres (e.g. Neon) closes connections that sit idle for a few
minutes. The app used to keep one permanent connection for LangGraph's saved
plan steps, so after any idle period every plan request failed with
"the connection is closed" until the server was restarted.

Run with:
    TEST_DATABASE_URL=postgresql://user:pass@localhost/tripbuddy_test?sslmode=disable \
        python -m pytest tests/test_db_reconnect.py -q
"""

import os
import sys

import pytest

TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_group_api import create_trip, env, join, person, vote  # noqa: E402,F401


def drop_app_connections():
    """What the database host does to idle connections: close them server-side."""
    import psycopg

    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )


@pytest.fixture
def connections_count_as_idle(env, monkeypatch):
    """Treat every pooled connection as idle, so it is checked before use
    (in real use that happens after IDLE_CHECK_SECONDS without traffic)."""
    import backend
    import db_pool
    import group_store

    monkeypatch.setattr(db_pool, "IDLE_CHECK_SECONDS", 0)
    yield
    # Leave healthy pools behind for the other test files
    for p in (group_store.pool(), getattr(backend, "checkpoint_pool", None)):
        if p is not None:
            p.check()


def test_app_recovers_after_the_database_closes_its_connections(env, connections_count_as_idle):
    client = env["client"]
    trip_id, varun = create_trip(client)
    bina = join(client, trip_id, person("Bina"))
    client.post(f"/api/trips/{trip_id}/plan", json={"member_id": varun})

    drop_app_connections()

    # Reading the trip uses both the trips tables and LangGraph's saved plan
    r = client.get(f"/api/trips/{trip_id}", params={"member_id": varun})
    assert r.status_code == 200
    assert r.json()["plan"]["requires_approval"] is True

    drop_app_connections()

    # Voting writes votes and resumes the saved plan
    vote(client, trip_id, varun, "approve")
    view = vote(client, trip_id, bina, "approve").json()
    assert view["trip"]["status"] == "completed"
