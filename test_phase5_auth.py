import io
import os
import re
import time
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from server import app, SESSIONS, AUTH_STORE, MAX_DEMO_UPLOAD_SIZE_BYTES
from auth_store import hash_password, verify_password, validate_password_strength


@pytest.fixture
def client():
    return TestClient(app)


def test_argon2id_password_hashing_and_verification():
    """Test Argon2id hashing produces distinct salts and validates correctly."""
    pw = "P@ssword123!"
    h1 = hash_password(pw)
    h2 = hash_password(pw)
    assert h1 != h2, "Argon2id must generate unique random salts for each hash"
    assert "$argon2id$" in h1
    assert verify_password(pw, h1) is True
    assert verify_password("WrongPassword123!", h1) is False


def test_password_strength_validation():
    """Password strength requires 8+ chars, uppercase, lowercase, and digit."""
    assert validate_password_strength("Short1A")[0] is False
    assert validate_password_strength("nouppercase123")[0] is False
    assert validate_password_strength("NOLOWERCASE123")[0] is False
    assert validate_password_strength("NoDigitsHere!")[0] is False
    assert validate_password_strength("ValidPass123")[0] is True


def test_user_registration_and_duplicate_prevention(client):
    """Test user registration, duplicate email rejection, and Argon2id hash storage."""
    email = f"user_{int(time.time() * 1000)}@example.com"
    pw = "StrongPass123!"

    # 1. Successful registration
    res = client.post("/api/auth/register", json={
        "email": email,
        "password": pw,
        "full_name": "Test User",
    })
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["user"]["email"] == email
    user_id = data["user"]["id"]
    assert user_id.startswith("usr_")

    # Verify password is not stored in plaintext in the database
    user_row = AUTH_STORE.get_raw_user_by_id(user_id)
    assert user_row is not None
    assert user_row["password_hash"] != pw
    assert "$argon2id$" in user_row["password_hash"]

    # 2. Duplicate email rejection
    dup_res = client.post("/api/auth/register", json={
        "email": email,
        "password": pw,
    })
    assert dup_res.status_code == 400
    assert "already exists" in dup_res.json()["detail"].lower()

    # 3. Invalid email rejection
    inv_res = client.post("/api/auth/register", json={
        "email": "not-an-email",
        "password": pw,
    })
    assert inv_res.status_code == 400


def test_login_logout_and_bearer_token_validation(client):
    """Test login credentials, Bearer auth, and token revocation upon logout."""
    email = f"auth_test_{int(time.time() * 1000)}@example.com"
    pw = "SecureAuthPass123!"

    # Register
    reg_res = client.post("/api/auth/register", json={"email": email, "password": pw})
    assert reg_res.status_code == 200

    # Login with wrong password
    bad_login = client.post("/api/auth/login", json={"email": email, "password": "WrongPassword123!"})
    assert bad_login.status_code == 401

    # Login with nonexistent user
    non_login = client.post("/api/auth/login", json={"email": "nonexistent@example.com", "password": pw})
    assert non_login.status_code == 401

    # Valid login
    login_res = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    assert token

    # Check /api/auth/me with Bearer token
    me_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 200
    assert me_res.json()["email"] == email

    # Logout (revokes token)
    logout_res = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout_res.status_code == 200

    # Check /api/auth/me with revoked token -> 401
    revoked_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert revoked_res.status_code == 401


def test_password_reset_workflow(client):
    """Test requesting a password reset token, updating password, and token invalidation."""
    email = f"reset_{int(time.time() * 1000)}@example.com"
    pw1 = "OriginalPass123!"
    pw2 = "BrandNewPass456!"

    # Register user
    reg = client.post("/api/auth/register", json={"email": email, "password": pw1})
    assert reg.status_code == 200
    old_token = reg.json()["access_token"]

    # Request reset token
    req_res = client.post("/api/auth/password-reset/request", json={"email": email})
    assert req_res.status_code == 200
    reset_token = req_res.json().get("reset_token")
    assert reset_token

    # Confirm password reset
    confirm_res = client.post("/api/auth/password-reset/confirm", json={
        "token": reset_token,
        "new_password": pw2,
    })
    assert confirm_res.status_code == 200

    # Old token was revoked during password reset
    me_old = client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    assert me_old.status_code == 401

    # Old password fails login
    old_login = client.post("/api/auth/login", json={"email": email, "password": pw1})
    assert old_login.status_code == 401

    # New password succeeds login
    new_login = client.post("/api/auth/login", json={"email": email, "password": pw2})
    assert new_login.status_code == 200

    # Reset token cannot be reused (one-time use)
    reuse_res = client.post("/api/auth/password-reset/confirm", json={
        "token": reset_token,
        "new_password": "AnotherPassword789!",
    })
    assert reuse_res.status_code == 400


def test_google_oauth_endpoints(client):
    """Test Google OAuth URL generation, state validation, and mocked token exchange."""
    # 1. When Google credentials are unset
    with patch("server.GOOGLE_CLIENT_ID", None):
        res = client.get("/api/auth/google/url")
        assert res.status_code == 503

    # 2. When configured
    with patch("server.GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com"):
        res = client.get("/api/auth/google/url")
        assert res.status_code == 200
        data = res.json()
        assert "auth_url" in data
        assert "accounts.google.com" in data["auth_url"]
        state = data["state"]
        assert AUTH_STORE.validate_and_consume_oauth_state(state) is True
        assert AUTH_STORE.validate_and_consume_oauth_state(state) is False  # State is one-time use

    # 3. Direct Google ID token exchange with mocked google.oauth2.id_token
    with patch("google.oauth2.id_token.verify_oauth2_token") as mock_verify:
        google_sub = f"google_sub_{int(time.time() * 1000)}"
        mock_verify.return_value = {
            "iss": "https://accounts.google.com",
            "aud": "mock-client-id",
            "sub": google_sub,
            "email": f"oauth_user_{int(time.time() * 1000)}@gmail.com",
            "name": "Google User",
            "email_verified": True,
        }
        with patch("server.GOOGLE_CLIENT_ID", "mock-client-id"):
            token_res = client.post("/api/auth/google/token", json={"id_token": "valid.mock.token"})
            assert token_res.status_code == 200
            token_data = token_res.json()
            assert "access_token" in token_data
            assert token_data["user"]["oauth_provider"] == "google"


def test_multi_tenant_data_isolation_uploads_and_sessions(client):
    """
    CRITICAL: Verify strict multi-tenant isolation.
    User A cannot access User B's uploaded datasets, sessions, or downloads.
    Anonymous users cannot access authenticated users' data.
    """
    # 1. Register User A and User B
    res_a = client.post("/api/auth/register", json={
        "email": f"usera_{int(time.time() * 1000)}@example.com",
        "password": "PasswordUserA123!",
    })
    token_a = res_a.json()["access_token"]
    user_a_id = res_a.json()["user"]["id"]

    res_b = client.post("/api/auth/register", json={
        "email": f"userb_{int(time.time() * 1000)}@example.com",
        "password": "PasswordUserB123!",
    })
    token_b = res_b.json()["access_token"]
    user_b_id = res_b.json()["user"]["id"]

    # 2. User A uploads an isolated dataset
    csv_content = "name,age,score\nAlice,30,95\nBob,25,88\n"
    upload_res = client.post(
        "/api/upload",
        files={"file": ("dataset_a.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert upload_res.status_code == 200
    file_a_path = upload_res.json()["filepath"]
    assert user_a_id in file_a_path

    # 3. User B attempts to access/profile User A's uploaded file -> 403 Forbidden!
    prof_b_res = client.post(
        "/api/profile",
        json={"filepath": file_a_path},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert prof_b_res.status_code == 403
    assert "permission" in prof_b_res.json()["detail"].lower()

    # 4. Anonymous user attempts to profile User A's uploaded file -> 403 Forbidden!
    anon_client = TestClient(app)
    prof_anon_res = anon_client.post(
        "/api/profile",
        json={"filepath": file_a_path},
    )
    assert prof_anon_res.status_code == 403

    # 5. User A profiles their own dataset -> 200 OK
    prof_a_res = client.post(
        "/api/profile",
        json={"filepath": file_a_path},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert prof_a_res.status_code == 200
    session_a_id = prof_a_res.json()["session_id"]

    # 6. User B attempts to access User A's active session -> 403 Forbidden!
    explore_b_res = client.post(
        "/api/dataset/explore",
        json={"session_id": session_a_id, "limit": 5},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert explore_b_res.status_code == 403

    # 7. Anonymous user attempts to access User A's session -> 401 Unauthorized!
    explore_anon_res = anon_client.post(
        "/api/dataset/explore",
        json={"session_id": session_a_id, "limit": 5},
    )
    assert explore_anon_res.status_code == 401

    # 8. User B attempts to download User A's cleaned dataset -> 403 Forbidden!
    down_b_res = client.get(
        f"/api/download/cleaned?session_id={session_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert down_b_res.status_code == 403

    # 9. User A accesses their own session -> 200 OK
    explore_a_res = client.post(
        "/api/dataset/explore",
        json={"session_id": session_a_id, "limit": 5},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert explore_a_res.status_code == 200

    # 10. List datasets isolation: User B's /api/datasets never lists User A's uploaded file
    list_b = client.get("/api/datasets", headers={"Authorization": f"Bearer {token_b}"}).json()["datasets"]
    paths_b = [d["path"] for d in list_b]
    assert file_a_path not in paths_b

    list_a = client.get("/api/datasets", headers={"Authorization": f"Bearer {token_a}"}).json()["datasets"]
    paths_a = [d["path"] for d in list_a]
    assert file_a_path in paths_a


def test_anonymous_demo_mode_limits_and_isolation(client):
    """
    Test Anonymous Demo Policy:
    - 10 MB upload limit for demo uploads.
    - 10,000 row limit for demo uploads.
    - Isolated sessions with session tokens.
    """
    # 1. Anonymous upload exceeds demo limit (MAX_DEMO_UPLOAD_SIZE_BYTES: 10 MB)
    oversized_bytes = b"a" * (MAX_DEMO_UPLOAD_SIZE_BYTES + 1024)
    oversized_res = client.post(
        "/api/upload",
        files={"file": ("large_demo.csv", io.BytesIO(oversized_bytes), "text/csv")},
    )
    assert oversized_res.status_code == 413
    assert "demo mode limit" in oversized_res.json()["detail"].lower()

    # 2. Anonymous upload within limits
    valid_csv = "id,value\n1,100\n2,200\n"
    anon_up = client.post(
        "/api/upload",
        files={"file": ("valid_demo.csv", io.BytesIO(valid_csv.encode("utf-8")), "text/csv")},
    )
    assert anon_up.status_code == 200
    demo_path = anon_up.json()["filepath"]
    assert "uploads/demo" in demo_path or "uploads\\demo" in demo_path

    # 3. Anonymous profiling establishes demo session with session_token
    prof_res = client.post("/api/profile", json={"filepath": demo_path})
    assert prof_res.status_code == 200
    demo_session_id = prof_res.json()["session_id"]
    demo_token = prof_res.json()["session_token"]
    assert demo_token

    # 4. Access with valid token -> 200 OK
    exp_ok = client.post(
        "/api/dataset/explore",
        json={"session_id": demo_session_id, "limit": 2},
        headers={"X-Session-Token": demo_token},
    )
    assert exp_ok.status_code == 200

    # 5. Access with invalid token -> 403 Forbidden
    exp_bad = client.post(
        "/api/dataset/explore",
        json={"session_id": demo_session_id, "limit": 2},
        headers={"X-Session-Token": "tampered_token_xyz"},
    )
    assert exp_bad.status_code == 403


def test_encrypted_user_preferences_and_gemini_key_protection(client):
    """
    Test storing custom API keys encrypted via Fernet in user preferences,
    ensuring responses never leak the plaintext secret key.
    """
    # Register user
    reg = client.post("/api/auth/register", json={
        "email": f"pref_{int(time.time() * 1000)}@example.com",
        "password": "PasswordPref123!",
    })
    token = reg.json()["access_token"]
    user_id = reg.json()["user"]["id"]

    # 1. Update preferences with custom API key
    custom_secret_key = "AIzaSyCustomUserSecretKey999XYZ"
    pref_res = client.post(
        "/api/user/preferences",
        json={
            "custom_api_key": custom_secret_key,
            "default_provider": "gemini",
            "default_model": "gemini-2.5-flash",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert pref_res.status_code == 200

    # 2. Verify key is encrypted in the SQLite database
    stored_enc = AUTH_STORE.get_encrypted_user_api_key(user_id)
    assert stored_enc is not None
    assert stored_enc != custom_secret_key
    assert "gAAAAA" in stored_enc, "Fernet encrypted tokens start with gAAAAA"

    # 3. Verify decryption matches
    decrypted = AUTH_STORE.get_decrypted_user_api_key(user_id)
    assert decrypted == custom_secret_key

    # 4. Verify GET /api/user/preferences never leaks full plaintext key
    get_pref = client.get("/api/user/preferences", headers={"Authorization": f"Bearer {token}"})
    assert get_pref.status_code == 200
    pref_data = get_pref.json()["preferences"]
    assert pref_data["has_custom_api_key"] is True
    assert custom_secret_key not in str(pref_data)
    assert pref_data["custom_api_key_preview"].startswith("AIzaSy...")


def test_user_sessions_history_endpoint(client):
    """Test /api/user/sessions returns only the authenticated user's sessions."""
    # Register user
    reg = client.post("/api/auth/register", json={
        "email": f"sessions_user_{int(time.time() * 1000)}@example.com",
        "password": "PasswordSessions123!",
    })
    token = reg.json()["access_token"]

    # Profile dirty sample dataset as this user
    prof_res = client.post(
        "/api/profile",
        json={"filepath": "data/benchmark/dirty_sample.csv"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert prof_res.status_code == 200
    session_id = prof_res.json()["session_id"]

    # Retrieve user sessions
    user_sess_res = client.get("/api/user/sessions", headers={"Authorization": f"Bearer {token}"})
    assert user_sess_res.status_code == 200
    sessions_list = user_sess_res.json()["sessions"]
    matching = [s for s in sessions_list if s["session_id"] == session_id]
    assert len(matching) == 1

    # Unauthenticated access to /api/user/sessions -> 401
    anon_client = TestClient(app)
    anon_sess_res = anon_client.get("/api/user/sessions")
    assert anon_sess_res.status_code == 401
