"""
Phase 6.2A Focused Test Suite: Authentication & AI Configuration
Verifies:
1. Gemini provider default server-key behavior and optional override
2. Claude/Anthropic 401 without personal key, success with personal key
3. OpenAI 401 without personal key, success with personal key
4. Cross-provider key isolation & contamination prevention
5. Ollama offline local execution without cloud keys & custom models
6. Model-provider compatibility validation (rejects mismatched models)
7. Per-provider API key storage, preview, and safe deletion
8. Multi-tenant isolation of stored credentials
9. Password reset token issuance, single-use, email dispatch, and session revocation
10. Google OAuth 503 behavior when unconfigured and CSRF state validation
"""

import io
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from server import app, AUTH_STORE, dispatch_password_reset_email
from auth_store import hash_password, verify_password
from llm_providers import (
    validate_provider_model_compatibility,
    validate_api_key_format_for_provider,
    COMPATIBLE_MODELS,
)


@pytest.fixture
def client():
    return TestClient(app)


def _create_user_and_session(client, email_prefix="user_62a"):
    """Helper to register user, upload CSV, and obtain session_id + token."""
    email = f"{email_prefix}_{int(time.time() * 1000)}@example.com"
    pw = "StrongPass123!"
    res = client.post("/api/auth/register", json={
        "email": email,
        "password": pw,
        "full_name": "Test User",
    })
    token = res.json()["access_token"]
    user_id = res.json()["user"]["id"]

    csv_data = "name,age,country\nAlice,30,USA\nBob,25,Canada\n"
    up_res = client.post(
        "/api/upload",
        files={"file": ("test.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")},
        headers={"Authorization": f"Bearer {token}"},
    )
    filepath = up_res.json()["filepath"]

    prof_res = client.post(
        "/api/profile",
        json={"filepath": filepath},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = prof_res.json()["session_id"]
    return {
        "email": email,
        "password": pw,
        "token": token,
        "user_id": user_id,
        "session_id": session_id,
        "filepath": filepath,
    }


# ==============================================================================
# 1. AI Provider Configuration & Key Isolation
# ==============================================================================

def test_gemini_with_server_key(client):
    """Gemini uses server-side GEMINI_API_KEY by default; personal key is optional."""
    user = _create_user_and_session(client, "gemini_srv")

    mock_plan_json = '{"summary": "Gemini clean", "actions": []}'
    with patch("llm_providers.GeminiProvider.generate_plan_json", return_value=mock_plan_json), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaSy_server_mock_key_12345"}):
        # Plan without providing personal api_key
        res = client.post(
            "/api/plan",
            json={
                "session_id": user["session_id"],
                "provider": "gemini",
                "model": "gemini-2.5-flash",
            },
            headers={"Authorization": f"Bearer {user['token']}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["summary"] == "Gemini clean"


def test_claude_without_personal_key(client):
    """Claude without personal API key fails with 401 Unauthorized."""
    user = _create_user_and_session(client, "claude_nokey")

    # Ensure no server anthropic key is set
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        res = client.post(
            "/api/plan",
            json={
                "session_id": user["session_id"],
                "provider": "anthropic",
                "model": "claude-3-5-sonnet-20241022",
            },
            headers={"Authorization": f"Bearer {user['token']}"},
        )
        assert res.status_code == 401
        assert "requires a personal API key" in res.json()["detail"]


def test_claude_with_personal_key(client):
    """Claude with valid sk-ant personal key succeeds in plan generation."""
    user = _create_user_and_session(client, "claude_key")

    mock_plan_json = '{"summary": "Claude clean", "actions": []}'
    with patch("llm_providers.AnthropicProvider.generate_plan_json", return_value=mock_plan_json):
        res = client.post(
            "/api/plan",
            json={
                "session_id": user["session_id"],
                "provider": "anthropic",
                "model": "claude-3-5-sonnet-20241022",
                "api_key": "sk-ant-api03-mocktestkey1234567890abcdef",
            },
            headers={"Authorization": f"Bearer {user['token']}"},
        )
        assert res.status_code == 200
        assert res.json()["summary"] == "Claude clean"


def test_openai_without_personal_key(client):
    """OpenAI without personal API key fails with 401 Unauthorized."""
    user = _create_user_and_session(client, "openai_nokey")

    with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
        res = client.post(
            "/api/plan",
            json={
                "session_id": user["session_id"],
                "provider": "openai",
                "model": "gpt-4o-mini",
            },
            headers={"Authorization": f"Bearer {user['token']}"},
        )
        assert res.status_code == 401
        assert "requires a personal API key" in res.json()["detail"]


def test_openai_with_personal_key(client):
    """OpenAI with valid sk- personal key succeeds in plan generation."""
    user = _create_user_and_session(client, "openai_key")

    mock_plan_json = '{"summary": "OpenAI clean", "actions": []}'
    with patch("llm_providers.OpenAIProvider.generate_plan_json", return_value=mock_plan_json):
        res = client.post(
            "/api/plan",
            json={
                "session_id": user["session_id"],
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "sk-proj-mocktestkey1234567890abcdef",
            },
            headers={"Authorization": f"Bearer {user['token']}"},
        )
        assert res.status_code == 200
        assert res.json()["summary"] == "OpenAI clean"


def test_cross_provider_key_contamination_prevented(client):
    """
    CRITICAL: Never cross-contaminate keys between providers.
    1. Saved Gemini key is not used for Claude or OpenAI requests.
    2. Invalid key formats for a specific provider are rejected with 400.
    """
    user = _create_user_and_session(client, "nocontam")

    # 1. User saves a Gemini personal key to their account
    pref_res = client.post(
        "/api/user/preferences",
        json={
            "api_key_provider": "gemini",
            "custom_api_key": "AIzaSy_gemini_personal_key_12345",
        },
        headers={"Authorization": f"Bearer {user['token']}"},
    )
    assert pref_res.status_code == 200

    # 2. When calling Anthropic, the stored Gemini key MUST NOT be passed to Anthropic
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        res_ant = client.post(
            "/api/plan",
            json={
                "session_id": user["session_id"],
                "provider": "anthropic",
                "model": "claude-3-5-sonnet-20241022",
            },
            headers={"Authorization": f"Bearer {user['token']}"},
        )
        assert res_ant.status_code == 401
        assert "requires a personal API key" in res_ant.json()["detail"]

    # 3. Explicitly passing a Gemini key (AIzaSy...) to Claude or OpenAI returns 400 Bad Request
    res_bad_claude = client.post(
        "/api/plan",
        json={
            "session_id": user["session_id"],
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-20241022",
            "api_key": "AIzaSy_wrong_format_for_anthropic",
        },
        headers={"Authorization": f"Bearer {user['token']}"},
    )
    assert res_bad_claude.status_code == 400
    assert "Never use one provider's key for another" in res_bad_claude.json()["detail"]

    res_bad_gemini = client.post(
        "/api/plan",
        json={
            "session_id": user["session_id"],
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "api_key": "sk-ant-api03-wrong-format-for-gemini",
        },
        headers={"Authorization": f"Bearer {user['token']}"},
    )
    assert res_bad_gemini.status_code == 400
    assert "Never use one provider's key for another" in res_bad_gemini.json()["detail"]


def test_ollama_without_cloud_credentials(client):
    """Ollama runs offline on local endpoint without cloud credentials and supports custom models."""
    user = _create_user_and_session(client, "ollama_user")

    mock_plan_json = '{"summary": "Ollama local clean", "actions": []}'
    with patch("llm_providers.OllamaProvider.generate_plan_json", return_value=mock_plan_json) as mock_gen:
        res = client.post(
            "/api/plan",
            json={
                "session_id": user["session_id"],
                "provider": "ollama",
                "model": "llama3.2",
                "base_url": "http://localhost:11434",
            },
            headers={"Authorization": f"Bearer {user['token']}"},
        )
        assert res.status_code == 200
        assert res.json()["summary"] == "Ollama local clean"

        # Verify custom model support for Ollama
        res_custom = client.post(
            "/api/plan",
            json={
                "session_id": user["session_id"],
                "provider": "ollama",
                "model": "custom-domain-cleaner:14b",
                "base_url": "http://192.168.1.50:11434",
            },
            headers={"Authorization": f"Bearer {user['token']}"},
        )
        assert res_custom.status_code == 200


def test_model_compatibility_validation(client):
    """Server enforces strict model-provider compatibility matrix and rejects invalid combinations."""
    user = _create_user_and_session(client, "compat_user")

    # Mismatched model 1: Anthropic with Gemini model
    res1 = client.post(
        "/api/plan",
        json={
            "session_id": user["session_id"],
            "provider": "anthropic",
            "model": "gemini-2.5-flash",
            "api_key": "sk-ant-mockkey1234567890",
        },
        headers={"Authorization": f"Bearer {user['token']}"},
    )
    assert res1.status_code == 400
    assert "incompatible with" in res1.json()["detail"].lower()

    # Mismatched model 2: Gemini with OpenAI model
    res2 = client.post(
        "/api/plan",
        json={
            "session_id": user["session_id"],
            "provider": "gemini",
            "model": "gpt-4o",
        },
        headers={"Authorization": f"Bearer {user['token']}"},
    )
    assert res2.status_code == 400
    assert "incompatible with" in res2.json()["detail"].lower()


def test_provider_settings_persistence(client):
    """User can save, preview, and selectively delete API keys per provider."""
    user = _create_user_and_session(client, "pref_user")

    # 1. Save OpenAI key
    res_set_oai = client.post(
        "/api/user/preferences",
        json={
            "api_key_provider": "openai",
            "custom_api_key": "sk-proj-testopenai1234567890abcdef",
            "default_provider": "openai",
            "default_model": "gpt-4o-mini",
        },
        headers={"Authorization": f"Bearer {user['token']}"},
    )
    assert res_set_oai.status_code == 200

    # 2. Save Anthropic key
    res_set_ant = client.post(
        "/api/user/preferences",
        json={
            "api_key_provider": "anthropic",
            "custom_api_key": "sk-ant-testclaude1234567890abcdef",
        },
        headers={"Authorization": f"Bearer {user['token']}"},
    )
    assert res_set_ant.status_code == 200

    # 3. Retrieve preferences - verifies previews are masked and ciphertexts stripped
    res_get = client.get(
        "/api/user/preferences",
        headers={"Authorization": f"Bearer {user['token']}"},
    )
    assert res_get.status_code == 200
    prefs = res_get.json()["preferences"]
    assert "encrypted_api_keys" not in prefs
    assert "encrypted_api_key" not in prefs
    assert "openai" in prefs["api_key_previews"]
    assert "anthropic" in prefs["api_key_previews"]
    assert prefs["api_key_previews"]["openai"].startswith("sk-pro...")
    assert prefs["api_key_previews"]["anthropic"].startswith("sk-ant...")

    # 4. Selectively clear OpenAI key only
    res_del_oai = client.post(
        "/api/user/preferences",
        json={
            "clear_api_key": True,
            "api_key_provider": "openai",
        },
        headers={"Authorization": f"Bearer {user['token']}"},
    )
    assert res_del_oai.status_code == 200
    updated_prefs = res_del_oai.json()["preferences"]
    assert "openai" not in updated_prefs.get("api_key_previews", {})
    assert "anthropic" in updated_prefs.get("api_key_previews", {})


def test_isolation_of_personal_api_keys(client):
    """Personal keys of User A are completely inaccessible to User B."""
    user_a = _create_user_and_session(client, "user_a")
    user_b = _create_user_and_session(client, "user_b")

    # User A saves an Anthropic key
    client.post(
        "/api/user/preferences",
        json={
            "api_key_provider": "anthropic",
            "custom_api_key": "sk-ant-usera-key1234567890abcdef",
        },
        headers={"Authorization": f"Bearer {user_a['token']}"},
    )

    # User B checks preferences
    res_b = client.get(
        "/api/user/preferences",
        headers={"Authorization": f"Bearer {user_b['token']}"},
    )
    prefs_b = res_b.json()["preferences"]
    assert "anthropic" not in prefs_b.get("api_key_previews", {})

    # User B attempts to plan with Anthropic without providing a key -> 401
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        res_plan_b = client.post(
            "/api/plan",
            json={
                "session_id": user_b["session_id"],
                "provider": "anthropic",
                "model": "claude-3-5-sonnet-20241022",
            },
            headers={"Authorization": f"Bearer {user_b['token']}"},
        )
        assert res_plan_b.status_code == 401


# ==============================================================================
# 2. Password Recovery & SMTP Dispatch
# ==============================================================================

def test_password_reset_workflow(client):
    """
    Password reset generates time-limited single-use token, dispatches email safely,
    updates password using Argon2id, and revokes all active session tokens.
    """
    email = f"reset_test_{int(time.time() * 1000)}@example.com"
    old_pw = "OldPassword123!"
    new_pw = "NewStrongPassword456!"

    # 1. Register user
    reg_res = client.post("/api/auth/register", json={
        "email": email,
        "password": old_pw,
        "full_name": "Reset Test User",
    })
    token_old = reg_res.json()["access_token"]

    # 2. Request reset token
    with patch("server.dispatch_password_reset_email") as mock_dispatch:
        mock_dispatch.return_value = True
        req_res = client.post("/api/auth/password-reset/request", json={"email": email})
        assert req_res.status_code == 200
        assert mock_dispatch.called

    # Extract generated reset token from non-prod response or store
    reset_token = req_res.json().get("dev_reset_token")
    assert reset_token is not None

    # 3. Complete password reset
    confirm_res = client.post("/api/auth/password-reset/confirm", json={
        "token": reset_token,
        "new_password": new_pw,
    })
    assert confirm_res.status_code == 200
    assert confirm_res.json()["status"] == "success"

    # 4. Single-use enforcement: Attempting to reuse the same token must fail
    reuse_res = client.post("/api/auth/password-reset/confirm", json={
        "token": reset_token,
        "new_password": "AnotherPassword789!",
    })
    assert reuse_res.status_code == 400

    # 5. Old password no longer works
    login_old = client.post("/api/auth/login", json={"email": email, "password": old_pw})
    assert login_old.status_code == 401

    # 6. New password succeeds
    login_new = client.post("/api/auth/login", json={"email": email, "password": new_pw})
    assert login_new.status_code == 200

    # 7. Old session token was revoked upon reset
    old_session_check = client.get(
        "/api/user/preferences",
        headers={"Authorization": f"Bearer {token_old}"},
    )
    assert old_session_check.status_code == 401


def test_smtp_dispatcher_configuration_handling():
    """SMTP dispatcher gracefully logs and does not throw when SMTP credentials are unconfigured."""
    # When SMTP is not configured, dispatch returns False and logs without raising
    with patch.dict(os.environ, {"SMTP_HOST": ""}):
        sent = dispatch_password_reset_email("user@example.com", "reset-token-xyz")
        assert sent is False


# ==============================================================================
# 3. Google OAuth Security & UI Accessibility
# ==============================================================================

def test_google_oauth_missing_config_behavior(client):
    """When GOOGLE_CLIENT_ID is not configured, endpoint returns 503 with clear explanation."""
    with patch("server.GOOGLE_CLIENT_ID", None):
        res = client.get("/api/auth/google/url")
        assert res.status_code == 503
        detail = res.json()["detail"]
        assert "Google authentication is temporarily unavailable" in detail
        assert "GOOGLE_CLIENT_ID" in detail


def test_google_oauth_state_validation_and_csrf():
    """OAuth state is cryptographically secure, one-time use, and rejects replay attacks."""
    state = AUTH_STORE.create_oauth_state()
    assert len(state) >= 32

    # Consuming for the first time succeeds
    assert AUTH_STORE.validate_and_consume_oauth_state(state) is True

    # Replay attack: consuming again returns False
    assert AUTH_STORE.validate_and_consume_oauth_state(state) is False

    # Tampered state fails
    assert AUTH_STORE.validate_and_consume_oauth_state("tampered_state_value") is False


def test_frontend_ui_auth_modal_structure(client):
    """Verify app.html keeps Google button visible across tabs and has return to sign in link."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    # Google section is shared and outside individual form tabs
    assert 'id="auth-google-section"' in html
    assert 'id="auth-btn-google"' in html
    assert 'Continue with Google' in html

    # Reset form has return to sign-in link
    assert 'switchAuthTab(\'login\')' in html
    assert 'Return to Sign In' in html

    # Engine settings drawer has model selector and custom model support
    assert 'id="settings-model-select"' in html
    assert 'id="settings-custom-model-wrap"' in html
    assert 'COMPATIBLE_MODELS_MAP' in html

    """When GOOGLE_CLIENT_ID is not configured, endpoint returns 503 with clear explanation."""
    with patch("server.GOOGLE_CLIENT_ID", None):
        res = client.get("/api/auth/google/url")
        assert res.status_code == 503
        detail = res.json()["detail"]
        assert "Google authentication is temporarily unavailable" in detail
        assert "GOOGLE_CLIENT_ID" in detail


def test_google_oauth_state_validation_and_csrf():
    """OAuth state is cryptographically secure, one-time use, and rejects replay attacks."""
    state = AUTH_STORE.create_oauth_state()
    assert len(state) >= 32

    # Consuming for the first time succeeds
    assert AUTH_STORE.validate_and_consume_oauth_state(state) is True

    # Replay attack: consuming again returns False
    assert AUTH_STORE.validate_and_consume_oauth_state(state) is False

    # Tampered state fails
    assert AUTH_STORE.validate_and_consume_oauth_state("tampered_state_value") is False


def test_frontend_ui_auth_modal_structure(client):
    """Verify app.html keeps Google button visible across tabs and has return to sign in link."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    # Google section is shared and outside individual form tabs
    assert 'id="auth-google-section"' in html
    assert 'id="auth-btn-google"' in html
    assert 'Continue with Google' in html

    # Reset form has return to sign-in link
    assert 'switchAuthTab(\'login\')' in html
    assert 'Return to Sign In' in html

    # Engine settings drawer has model selector and custom model support
    assert 'id="settings-model-select"' in html
    assert 'id="settings-custom-model-wrap"' in html
    assert 'COMPATIBLE_MODELS_MAP' in html
