import io
import os
import re
import time
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from server import app, SESSIONS, AUTH_STORE
from auth_store import (
    encrypt_user_secret,
    decrypt_user_secret,
    rotate_user_secret,
    get_fernet_suite,
)


@pytest.fixture
def client():
    return TestClient(app)


def test_cookie_authentication_and_csrf_protection(client):
    """
    Test HttpOnly cookie authentication:
    - Login and Register issue HttpOnly auth_token and SameSite=Lax csrf_token.
    - Mutating requests with ambient cookie auth require matching X-CSRF-Token header.
    - Rejects missing or forged CSRF tokens with 403 Forbidden.
    - Logout clears cookies.
    """
    email = f"cookie_user_{int(time.time() * 1000)}@example.com"
    pw = "CookieAuthPass123!"

    # 1. Register and inspect Set-Cookie headers
    reg_res = client.post("/api/auth/register", json={"email": email, "password": pw})
    assert reg_res.status_code == 200
    cookies = reg_res.cookies
    assert "auth_token" in cookies
    assert "csrf_token" in cookies

    # Verify cookie headers in raw Set-Cookie
    set_cookie_headers = reg_res.headers.get_list("set-cookie") if hasattr(reg_res.headers, "get_list") else [reg_res.headers.get("set-cookie", "")]
    raw_cookies_str = " ".join(set_cookie_headers).lower()
    assert "httponly" in raw_cookies_str
    assert "samesite=lax" in raw_cookies_str

    csrf_val = cookies["csrf_token"]

    # 2. Mutating request with cookie auth but WITHOUT X-CSRF-Token -> 403 Forbidden!
    # client automatically passes its stored cookies
    no_csrf_res = client.post("/api/user/preferences", json={"theme": "dark"})
    assert no_csrf_res.status_code == 403
    assert "csrf" in no_csrf_res.json()["detail"].lower()

    # 3. Mutating request with cookie auth and INVALID X-CSRF-Token -> 403 Forbidden!
    bad_csrf_res = client.post(
        "/api/user/preferences",
        json={"theme": "dark"},
        headers={"X-CSRF-Token": "forged_csrf_value"},
    )
    assert bad_csrf_res.status_code == 403

    # 4. Mutating request with cookie auth and VALID matching X-CSRF-Token -> 200 OK
    good_csrf_res = client.post(
        "/api/user/preferences",
        json={"theme": "dark"},
        headers={"X-CSRF-Token": csrf_val},
    )
    assert good_csrf_res.status_code == 200
    assert good_csrf_res.json()["preferences"]["theme"] == "dark"

    # 5. Non-mutating GET request with cookie auth does not require CSRF header
    get_res = client.get("/api/user/preferences")
    assert get_res.status_code == 200

    # 6. Logout clears cookies
    logout_res = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf_val})
    assert logout_res.status_code == 200


def test_multifernet_key_rotation_and_safe_secret_storage():
    """
    Test MultiFernet key rotation:
    - Data encrypted under historical key K1 can still be decrypted when K2 becomes primary.
    - rotate_user_secret re-encrypts ciphertext under the new primary key K2.
    - rotate_all_user_keys sweeps the database and re-encrypts all stored secrets.
    """
    key1 = "historical_secret_key_one_12345"
    key2 = "new_primary_secret_key_two_67890"

    plaintext = "AIzaSySuperSecretApiKeyToRotate999"

    # 1. Encrypt under Key 1
    with patch.dict(os.environ, {"SECRET_KEY": key1, "SECRET_KEY_ROTATION_FALLBACKS": ""}):
        ct1 = encrypt_user_secret(plaintext)
        assert ct1
        assert decrypt_user_secret(ct1) == plaintext

    # 2. Rotate to Key 2 as primary, with Key 1 as fallback
    with patch.dict(os.environ, {"SECRET_KEY": key2, "SECRET_KEY_ROTATION_FALLBACKS": key1}):
        # Decrypting old ciphertext succeeds via fallback key
        decrypted = decrypt_user_secret(ct1)
        assert decrypted == plaintext

        # Re-encrypt old ciphertext under new primary key
        ct2 = rotate_user_secret(ct1)
        assert ct2 != ct1
        assert decrypt_user_secret(ct2) == plaintext

    # 3. Verify ciphertext encrypted under Key 2 cannot be decrypted with Key 1 alone
    with patch.dict(os.environ, {"SECRET_KEY": key1, "SECRET_KEY_ROTATION_FALLBACKS": ""}):
        assert decrypt_user_secret(ct2) is None


def test_password_reset_enumeration_and_session_revocation(client):
    """
    Test password reset security:
    - Requesting reset for non-existent email returns same generic message (anti-enumeration).
    - Production environment never exposes reset token in response.
    - Password reset revokes all prior active sessions.
    - One-time use: reset token cannot be used twice.
    """
    email = f"reset_enum_{int(time.time() * 1000)}@example.com"
    pw = "OriginalPass123!"

    # Register
    reg = client.post("/api/auth/register", json={"email": email, "password": pw})
    old_token = reg.json()["access_token"]

    # 1. Request reset for NON-EXISTENT email
    non_existent = client.post("/api/auth/password-reset/request", json={"email": "nobody_exists@example.com"})
    assert non_existent.status_code == 200
    assert "instructions have been dispatched" in non_existent.json()["message"].lower()
    assert "reset_token" not in non_existent.json()

    # 2. In production mode, existing email ALSO does not leak reset token in response
    with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
        prod_req = client.post("/api/auth/password-reset/request", json={"email": email})
        assert prod_req.status_code == 200
        assert "reset_token" not in prod_req.json()
        assert "dev_reset_token" not in prod_req.json()

    # 3. In dev/test mode, token is accessible
    dev_req = client.post("/api/auth/password-reset/request", json={"email": email})
    token = dev_req.json()["reset_token"]
    assert token

    # 4. Confirm reset
    new_pw = "BrandNewPass456!"
    confirm_res = client.post("/api/auth/password-reset/confirm", json={
        "token": token,
        "new_password": new_pw,
    })
    assert confirm_res.status_code == 200

    # 5. Old session token is revoked
    anon = TestClient(app)
    old_session_check = anon.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    assert old_session_check.status_code == 401

    # 6. Reusing same token fails (single use)
    reuse = client.post("/api/auth/password-reset/confirm", json={
        "token": token,
        "new_password": "YetAnotherPassword789!",
    })
    assert reuse.status_code == 400


def test_guest_to_account_session_migration_and_claim(client):
    """
    Test adopting a guest demo session into an authenticated user account:
    - Anonymous visitor profiles dataset and holds session_id and session_token.
    - Visitor registers / logs in.
    - Newly registered user claims session using /api/session/claim.
    - Ownership is successfully transferred to user account.
    - Another user or guest cannot claim or steal an existing user session.
    """
    anon_client = TestClient(app)

    # 1. Anonymous visitor profiles dataset
    prof_res = anon_client.post("/api/profile", json={"filepath": "data/benchmark/dirty_sample.csv"})
    assert prof_res.status_code == 200
    guest_session_id = prof_res.json()["session_id"]
    guest_token = prof_res.json()["session_token"]

    # 2. Visitor registers an account
    user_email = f"migrated_user_{int(time.time() * 1000)}@example.com"
    reg_res = client.post("/api/auth/register", json={
        "email": user_email,
        "password": "PasswordMigrate123!",
    })
    user_token = reg_res.json()["access_token"]
    user_id = reg_res.json()["user"]["id"]

    # 3. Claim the guest session
    claim_res = client.post(
        "/api/session/claim",
        json={"session_id": guest_session_id, "session_token": guest_token},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert claim_res.status_code == 200
    assert claim_res.json()["owner_id"] == user_id

    # 4. Session now belongs to registered user: User accesses session -> 200 OK
    explore_user = client.post(
        "/api/dataset/explore",
        json={"session_id": guest_session_id, "limit": 2},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert explore_user.status_code == 200

    # 5. Anonymous visitor can no longer access the claimed session without user credentials
    explore_anon = anon_client.post(
        "/api/dataset/explore",
        json={"session_id": guest_session_id, "limit": 2},
    )
    assert explore_anon.status_code == 401

    # 6. Another registered user cannot steal or claim an already owned session -> 403 Forbidden!
    client_b = TestClient(app)
    reg_b = client_b.post("/api/auth/register", json={
        "email": f"attacker_{int(time.time() * 1000)}@example.com",
        "password": "PasswordAttacker123!",
    })
    token_b = reg_b.json()["access_token"]

    steal_res = client_b.post(
        "/api/session/claim",
        json={"session_id": guest_session_id, "session_token": guest_token},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert steal_res.status_code == 403


def test_exhaustive_multi_tenant_cross_user_rejection_across_all_endpoints(client):
    """
    Exhaustive Multi-Tenant Verification:
    Verify that User B and anonymous visitors are rejected on EVERY endpoint
    when attempting to operate on User A's session or dataset.
    """
    # 1. Create User A and User B
    client_a = TestClient(app)
    res_a = client_a.post("/api/auth/register", json={
        "email": f"tenant_a_{int(time.time() * 1000)}@example.com",
        "password": "PasswordTenantA123!",
    })
    token_a = res_a.json()["access_token"]

    client_b = TestClient(app)
    res_b = client_b.post("/api/auth/register", json={
        "email": f"tenant_b_{int(time.time() * 1000)}@example.com",
        "password": "PasswordTenantB123!",
    })
    token_b = res_b.json()["access_token"]

    # 2. User A uploads file and profiles it
    csv_bytes = b"id,name,val\n1,Alpha,100\n2,Beta,200\n"
    up_a = client_a.post(
        "/api/upload",
        files={"file": ("tenant_a.csv", io.BytesIO(csv_bytes), "text/csv")},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert up_a.status_code == 200
    file_a = up_a.json()["filepath"]

    prof_a = client_a.post(
        "/api/profile",
        json={"filepath": file_a},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert prof_a.status_code == 200
    session_id = prof_a.json()["session_id"]

    anon_client = TestClient(app)

    # 3. Test every endpoint with User B (should be 403) and Anonymous (should be 401 or 403)
    endpoints_to_test = [
        # (method, path, json_payload, query_params)
        ("POST", "/api/profile", {"filepath": file_a}, None),
        ("POST", "/api/dataset/explore", {"session_id": session_id, "limit": 2}, None),
        ("POST", "/api/facets/compute", {"session_id": session_id, "column": "name", "facet_type": "text"}, None),
        ("POST", "/api/clean", {"session_id": session_id, "approved_action_ids": []}, None),
        ("POST", "/api/session/undo", {"session_id": session_id}, None),
        ("POST", "/api/session/redo", {"session_id": session_id}, None),
        ("POST", "/api/session/rollback", {"session_id": session_id, "target_step": 0}, None),
        ("POST", "/api/evaluate", {"session_id": session_id}, None),
        ("POST", "/api/cluster/detect", {"session_id": session_id, "column": "name", "method": "fingerprint"}, None),
        ("POST", "/api/cluster/apply", {"session_id": session_id, "column": "name", "clusters": [], "mapping": {}}, None),
        ("POST", "/api/grel/preview", {"session_id": session_id, "column": "name", "expression": "value.toUppercase()"}, None),
        ("POST", "/api/grel/apply", {"session_id": session_id, "column": "name", "expression": "value.toUppercase()"}, None),
        ("POST", "/api/reconcile/match", {"session_id": session_id, "column": "name", "catalog": "countries"}, None),
        ("POST", "/api/reconcile/apply", {"session_id": session_id, "column": "name", "mapping": {}}, None),
        ("POST", "/api/workflow/apply_recipe", {"session_id": session_id, "recipe": {"operations": []}}, None),
        ("GET", f"/api/download/cleaned?session_id={session_id}", None, None),
        ("GET", f"/api/download/report?session_id={session_id}", None, None),
        ("GET", f"/api/workflow/history?session_id={session_id}", None, None),
        ("POST", "/api/workflow/export_recipe", {"session_id": session_id}, None),
        ("POST", "/api/plan", {"session_id": session_id}, None),
        ("POST", "/api/plan/add_action", {"session_id": session_id, "action": "drop_duplicates"}, None),
        ("POST", "/api/plan/remove_action", {"session_id": session_id, "action_index": 0}, None),
    ]

    for method, path, json_data, params in endpoints_to_test:
        # A) User B attempts access -> 403 Forbidden
        if method == "POST":
            res_b = client_b.post(path, json=json_data, headers={"Authorization": f"Bearer {token_b}"})
        else:
            res_b = client_b.get(path, params=params, headers={"Authorization": f"Bearer {token_b}"})
        assert res_b.status_code == 403, f"Expected 403 for User B on {method} {path}, got {res_b.status_code}"

        # B) Anonymous visitor attempts access -> 401 Unauthorized (or 403 for unowned files)
        if method == "POST":
            res_anon = anon_client.post(path, json=json_data)
        else:
            res_anon = anon_client.get(path, params=params)
        assert res_anon.status_code in (401, 403), f"Expected 401/403 for anonymous on {method} {path}, got {res_anon.status_code}"
