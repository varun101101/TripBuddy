"""
Tests for Clerk accounts and "My trips": real PostgreSQL, fake AI model.

Clerk itself isn't contacted. Instead the tests make their own RSA key pair,
sign tokens shaped like Clerk session tokens with it, and give the server the
public half as CLERK_JWT_KEY, exactly like Clerk's "JWKS Public Key" setting.

Run with the same throwaway database as the group tests:
    TEST_DATABASE_URL=postgresql://user:pass@localhost/tripbuddy_test?sslmode=disable \
        python -m pytest tests/test_accounts.py -q
"""

import base64
import os
import sys
import time
import uuid

import pytest

TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_group_api import create_trip, env, join, person  # noqa: E402,F401

FAPI = "tripbuddy-test.clerk.accounts.dev"
PUBLISHABLE_KEY = "pk_test_" + base64.b64encode(f"{FAPI}$".encode()).decode().rstrip("=")
SITE = "http://testserver"  # the TestClient's origin


def _key_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private, public_pem


CLERK_PRIVATE_KEY, CLERK_PUBLIC_PEM = _key_pair()
OTHER_PRIVATE_KEY, _ = _key_pair()


@pytest.fixture(autouse=True)
def clerk_settings(monkeypatch):
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", PUBLISHABLE_KEY)
    monkeypatch.setenv("CLERK_JWT_KEY", CLERK_PUBLIC_PEM)
    monkeypatch.delenv("CLERK_AUTHORIZED_PARTIES", raising=False)


def clerk_token(user_id, key=None, azp=SITE, expires_in=300, algorithm="RS256"):
    import jwt

    now = int(time.time())
    claims = {"sub": user_id, "sid": "sess_" + uuid.uuid4().hex[:8], "iat": now,
              "nbf": now - 5, "exp": now + expires_in,
              "iss": f"https://{FAPI}"}
    if azp:
        claims["azp"] = azp
    return jwt.encode(claims, key or CLERK_PRIVATE_KEY, algorithm=algorithm)


def device(user_id=None):
    """A browser. With user_id it's signed in as that Clerk user."""
    import app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    if user_id:
        client.headers["Authorization"] = f"Bearer {clerk_token(user_id)}"
    return client


def new_user():
    return "user_" + uuid.uuid4().hex[:12]


def test_frontend_api_comes_from_the_publishable_key():
    import accounts

    assert accounts.frontend_api(PUBLISHABLE_KEY) == FAPI
    assert accounts.frontend_api("pk_test_not-base64!!") is None
    assert accounts.frontend_api("sk_test_abc") is None


def test_pages_load_clerk_only_when_configured(env, monkeypatch):
    page = device().get("/login").text
    assert f"https://{FAPI}/npm/@clerk/clerk-js@6/dist/clerk.browser.js" in page
    assert f'data-clerk-publishable-key="{PUBLISHABLE_KEY}"' in page
    assert "/static/account.js" in device().get("/").text

    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY")
    page = device().get("/").text
    assert "clerk.browser.js" not in page and "window.TRIPBUDDY_CLERK = null" in page


def test_only_genuine_clerk_tokens_for_this_site_are_accepted(env):
    import jwt

    user = new_user()
    assert device(user).get("/api/me/trips").status_code == 200

    def status_with(token):
        return device().get("/api/me/trips", headers={"Authorization": f"Bearer {token}"}).status_code

    assert status_with(clerk_token(user, expires_in=-60)) == 401          # expired
    assert status_with(clerk_token(user, key=OTHER_PRIVATE_KEY)) == 401   # not signed by Clerk
    assert status_with(clerk_token(user, azp="https://evil.example")) == 401  # made for another site
    assert status_with(clerk_token(user, azp=None)) == 200                # no azp claim: allowed
    forged = jwt.encode({"sub": user, "exp": int(time.time()) + 300},
                        "an-attacker-guessed-hmac-secret-of-32-bytes", algorithm="HS256")
    assert status_with(forged) == 401                                      # wrong algorithm
    assert device().get("/api/me/trips").status_code == 401                # guest

    # Clerk's __session cookie also works for same-site requests
    browser = device()
    browser.cookies.set("__session", clerk_token(user))
    assert browser.get("/api/me/trips").status_code == 200


def test_without_clerk_settings_everyone_is_a_guest(env, monkeypatch):
    user = new_user()
    token = clerk_token(user)
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY")
    monkeypatch.delenv("CLERK_JWT_KEY")
    r = device().get("/api/me/trips", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_group_trips_saved_and_opened_on_another_device(env):
    varun = new_user()
    laptop = device(varun)
    trip_id, member_id = create_trip(laptop)

    join(device(), trip_id, person("Riya"))  # a friend joins as a guest

    trips = laptop.get("/api/me/trips").json()["group_trips"]
    assert [(t["trip_id"], t["is_organiser"], t["members"]) for t in trips] == [(trip_id, True, 2)]

    phone = device(varun)  # same Clerk user, no member key saved on this device
    view = phone.get(f"/api/trips/{trip_id}").json()
    assert view["you"]["name"] == "Varun"
    assert view["you"]["member_id"] == member_id

    r = phone.post(f"/api/trips/{trip_id}/members", json=person("Varun again"))
    assert r.status_code == 409


def test_guest_membership_is_saved_after_signing_in(env):
    trip_id, _ = create_trip(device(new_user()))

    neha = new_user()
    browser = device()
    neha_key = join(browser, trip_id, person("Neha"))  # joined as a guest
    browser.headers["Authorization"] = f"Bearer {clerk_token(neha)}"  # then signs in
    assert browser.get("/api/me/trips").json()["group_trips"] == []

    view = browser.get(f"/api/trips/{trip_id}", params={"member_id": neha_key}).json()
    assert view["you"]["name"] == "Neha"
    assert [t["trip_id"] for t in browser.get("/api/me/trips").json()["group_trips"]] == [trip_id]


def test_solo_plans_saved_for_signed_in_users_only(env):
    import psycopg

    user = new_user()
    client = device(user)
    body = client.post("/api/travel", json={"message": "Plan 3 days in Goa from Mumbai"}).json()
    assert body["requires_approval"]
    plans = client.get("/api/me/trips").json()["solo_plans"]
    assert [(p["thread_id"], p["status"], p["title"]) for p in plans] == [
        (body["thread_id"], "draft", "Plan 3 days in Goa from Mumbai")]

    client.post("/api/travel/approve",
                json={"thread_id": body["thread_id"], "approved": True, "feedback": ""})
    assert client.get("/api/me/trips").json()["solo_plans"][0]["status"] == "final"
    opened = client.get(f"/api/me/plans/{body['thread_id']}").json()
    assert opened["answer"] == "FINAL PLAN" and opened["requires_approval"] is False

    # Stored in PostgreSQL under the Clerk user ID
    with psycopg.connect(TEST_DB) as conn:
        owner = conn.execute("SELECT clerk_user_id FROM solo_plans WHERE thread_id = %s",
                             (body["thread_id"],)).fetchone()[0]
    assert owner == user

    assert device(new_user()).get(f"/api/me/plans/{body['thread_id']}").status_code == 404

    guest = device()
    assert guest.post("/api/travel", json={"message": "Plan 2 days in Udaipur"}).json()["success"]
    assert guest.get("/api/me/trips").status_code == 401


# ---------------------------------------------------------------------
# Deleting from "My trips"
# ---------------------------------------------------------------------
def _checkpoint_rows(thread_id):
    import psycopg

    with psycopg.connect(TEST_DB) as conn:
        return conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s",
                            (thread_id,)).fetchone()[0]


def test_deleting_a_solo_plan_removes_it_and_its_saved_steps(env):
    user = new_user()
    client = device(user)
    thread = client.post("/api/travel", json={"message": "Plan 2 days in Udaipur"}).json()["thread_id"]
    assert _checkpoint_rows(thread) > 0

    assert device().delete(f"/api/me/plans/{thread}").status_code == 401            # guest
    assert device(new_user()).delete(f"/api/me/plans/{thread}").status_code == 404  # someone else

    assert client.delete(f"/api/me/plans/{thread}").json()["success"]
    assert client.get("/api/me/trips").json()["solo_plans"] == []
    assert client.get(f"/api/me/plans/{thread}").status_code == 404
    assert _checkpoint_rows(thread) == 0


def test_organiser_deletes_a_group_trip_for_everyone(env):
    varun = new_user()
    organiser = device(varun)
    trip_id, organiser_key = create_trip(organiser)
    riya = new_user()
    join(device(riya), trip_id, person("Riya"))
    organiser.post(f"/api/trips/{trip_id}/plan", json={"member_id": organiser_key})
    assert _checkpoint_rows(f"trip_{trip_id}_1") > 0

    r = organiser.delete(f"/api/me/trips/{trip_id}")
    assert r.json() == {"success": True, "outcome": "deleted"}
    assert organiser.get(f"/api/trips/{trip_id}").status_code == 404
    assert device(riya).get("/api/me/trips").json()["group_trips"] == []
    assert _checkpoint_rows(f"trip_{trip_id}_1") == 0


def test_member_removes_a_group_trip_only_from_their_own_list(env):
    varun, riya = new_user(), new_user()
    trip_id, _ = create_trip(device(varun))
    riya_device = device(riya)
    riya_key = join(riya_device, trip_id, person("Riya"))

    r = riya_device.delete(f"/api/me/trips/{trip_id}")
    assert r.json() == {"success": True, "outcome": "removed"}
    assert riya_device.get("/api/me/trips").json()["group_trips"] == []

    # The trip and Riya's place in it are untouched, so votes still work
    trips = device(varun).get("/api/me/trips").json()["group_trips"]
    assert [(t["trip_id"], t["members"]) for t in trips] == [(trip_id, 2)]
    guest_view = device().get(f"/api/trips/{trip_id}", params={"member_id": riya_key}).json()
    assert guest_view["you"]["name"] == "Riya"

    assert device(new_user()).delete(f"/api/me/trips/{trip_id}").status_code == 404


def test_trip_cannot_be_deleted_while_agents_are_working_on_it(env):
    import psycopg

    varun = new_user()
    organiser = device(varun)
    trip_id, _ = create_trip(organiser)
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("UPDATE trips SET status = 'planning', updated_at = now() WHERE trip_id = %s",
                     (trip_id,))
    assert organiser.delete(f"/api/me/trips/{trip_id}").status_code == 409
    assert organiser.get(f"/api/trips/{trip_id}").status_code == 200
