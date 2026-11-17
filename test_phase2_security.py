"""
Comprehensive Test Suite for Phase 2: Critical Security Hardening.

Covers:
1. XSS Payloads in CSV Headers & Values (DOM escaping and HTML injection protection).
2. API Key Leakage & Sanitization (repr masking, error scrubbing, response inspection).
3. Gemini Server-Key Fallback Behavior (fallback enabled vs disabled).
4. Gemini Key Override Behavior (user key override, environment immutability).
5. IP-Based Rate Limiting (per-minute burst limits, hourly server-key quota, 429 Retry-After).
6. CORS Restrictions (production rejection of '*', allowed origins validation).
7. Security Response Headers (CSP, X-Content-Type-Options, X-Frame-Options, HSTS).
8. Docker Hardening & Non-Root Execution (Dockerfile user, directory permissions, dynamic PORT).
9. Production Configuration Validation.
"""

import io
import os
import re
import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from fastapi.testclient import TestClient

from server import app, PlanRequest, SESSIONS
from rate_limiter import IPRateLimiter, enforce_ai_rate_limits, limiter
from security_utils import (
    sanitize_secret_strings,
    validate_cors_origins,
    DEFAULT_CSP,
)
from llm_providers import GeminiProvider, BaseLLMProvider


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset rate limiter state before every test to guarantee clean isolation."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


# ==============================================================================
# 1. XSS PAYLOADS IN CSV HEADERS & VALUES
# ==============================================================================

def test_xss_in_csv_header_profiling_and_preview(client):
    """
    Verify that CSV files with malicious script tags in headers are safely handled
    and cannot cause unescaped script execution.
    """
    malicious_header_csv = (
        '<script>alert("xss-header")</script>,age,city\n'
        'Alice,30,New York\n'
        'Bob,25,San Francisco\n'
    )
    file_bytes = io.BytesIO(malicious_header_csv.encode("utf-8"))

    upload_res = client.post(
        "/api/upload",
        files={"file": ("test_xss.csv", file_bytes, "text/csv")},
    )
    assert upload_res.status_code == 200
    filepath = upload_res.json()["filepath"]

    prof_res = client.post("/api/profile", json={"filepath": filepath})
    assert prof_res.status_code == 200
    prof_data = prof_res.json()
    session_id = prof_data["session_id"]

    col_names = [c["name"] for c in prof_data["columns"]]
    assert '<script>alert("xss-header")</script>' in col_names

    # Request preview records: verify JSON serialization and exploration does not break
    preview_rows = prof_data.get("preview_rows", [])
    assert len(preview_rows) == 2
    assert '<script>alert("xss-header")</script>' in preview_rows[0]

    explore_res = client.post("/api/dataset/explore", json={"session_id": session_id, "limit": 5})
    assert explore_res.status_code == 200
    explore_data = explore_res.json()
    assert any('<script>alert("xss-header")</script>' in col for col in explore_data["columns"])


def test_xss_in_csv_cell_values(client):
    """
    Verify that CSV files containing HTML/JS injection in cell values are preserved
    as literal strings without active script execution.
    """
    malicious_cells_csv = (
        'id,payload,notes\n'
        '1,<img src=x onerror=alert(1)>,test1\n'
        '2,"<svg/onload=alert(\'xss\')>",test2\n'
        '3,javascript:alert(document.cookie),test3\n'
    )
    file_bytes = io.BytesIO(malicious_cells_csv.encode("utf-8"))

    upload_res = client.post(
        "/api/upload",
        files={"file": ("malicious_cells.csv", file_bytes, "text/csv")},
    )
    assert upload_res.status_code == 200
    filepath = upload_res.json()["filepath"]

    prof_res = client.post("/api/profile", json={"filepath": filepath})
    assert prof_res.status_code == 200
    prof_data = prof_res.json()
    session_id = prof_data["session_id"]

    preview_rows = prof_data.get("preview_rows", [])
    assert len(preview_rows) == 3
    assert preview_rows[0]["payload"] == "<img src=x onerror=alert(1)>"

    explore_res = client.post("/api/dataset/explore", json={"session_id": session_id, "limit": 5})
    assert explore_res.status_code == 200
    records = explore_res.json()["records"]
    assert records[0]["payload"] == "<img src=x onerror=alert(1)>"


def test_frontend_app_html_contains_escape_html_everywhere():
    """
    Verify that static/app.html properly defines escapeHtml at the top of script
    and interpolates column headers and top values through escapeHtml.
    """
    with open("static/app.html", "r", encoding="utf-8") as f:
        html = f.read()

    # 1. escapeHtml definition exists
    assert "function escapeHtml(str)" in html

    # 2. Key vulnerable points identified in audit now use escapeHtml
    assert "${escapeHtml(col.name)}" in html
    assert "${escapeHtml(String(item.value" in html or "${escapeHtml(item.value" in html
    assert "${escapeHtml(formatActionName(act.action))}" in html
    assert "${escapeHtml(colName)}" in html


# ==============================================================================
# 2. API KEY LEAKAGE & SANITIZATION
# ==============================================================================

def test_sanitize_secret_strings_patterns():
    """Verify that sanitize_secret_strings redacts Gemini, OpenAI, Anthropic, and Bearer tokens."""
    raw_gemini = "Error contacting API with key AIzaSyA1234567890123456789012345678901b: Quota exceeded"
    sanitized_gemini = sanitize_secret_strings(raw_gemini)
    assert "AIzaSy" not in sanitized_gemini
    assert "[REDACTED_GEMINI_KEY]" in sanitized_gemini

    raw_openai = "OpenAI failed with sk-abcdefghijklmnopqrstuvwxyz1234567890"
    sanitized_openai = sanitize_secret_strings(raw_openai)
    assert "sk-abcdef" not in sanitized_openai
    assert "[REDACTED_OPENAI_KEY]" in sanitized_openai

    raw_anthropic = "Anthropic key sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456 was rejected"
    sanitized_anthropic = sanitize_secret_strings(raw_anthropic)
    assert "sk-ant-" not in sanitized_anthropic
    assert "[REDACTED_ANTHROPIC_KEY]" in sanitized_anthropic

    raw_bearer = "Authorization: Bearer mySecretToken1234567890"
    sanitized_bearer = sanitize_secret_strings(raw_bearer)
    assert "mySecretToken" not in sanitized_bearer
    assert "[REDACTED_TOKEN]" in sanitized_bearer


def test_plan_request_repr_masks_api_key():
    """Verify that printing or logging PlanRequest never exposes the api_key field."""
    req = PlanRequest(
        session_id="test-session",
        provider="gemini",
        api_key="AIzaSySecretKey1234567890123456789012",
    )
    repr_str = repr(req)
    assert "AIzaSySecretKey" not in repr_str
    assert "test-session" in repr_str


def test_base_llm_provider_repr_masks_api_key():
    """Verify that printing or logging a provider instance never exposes its api_key."""
    provider = GeminiProvider(
        model_name="gemini-2.5-flash",
        api_key="AIzaSyTestKey1234567890123456789012345",
        client=MagicMock(),
    )
    repr_str = repr(provider)
    assert "AIzaSy" not in repr_str
    assert "[CONFIGURED]" in repr_str


def test_api_plan_response_never_contains_api_key(client):
    """Verify that /api/plan response JSON never returns api_key."""
    dirty_path = "data/benchmark/dirty_sample.csv"
    prof_res = client.post("/api/profile", json={"filepath": dirty_path})
    session_id = prof_res.json()["session_id"]

    secret_key = "AIzaSyTestKeyForExclusion9876543210123"
    plan_res = client.post(
        "/api/plan",
        json={
            "session_id": session_id,
            "provider": "mock",
            "api_key": secret_key,
        },
    )
    assert plan_res.status_code == 200
    plan_data = plan_res.json()
    assert "api_key" not in plan_data
    assert secret_key not in str(plan_data)


# ==============================================================================
# 3. GEMINI SERVER-KEY FALLBACK BEHAVIOR
# ==============================================================================

def test_gemini_server_key_fallback_when_allowed(client):
    """
    Verify that when ALLOW_SERVER_GEMINI_FALLBACK=true, planning succeeds using
    the configured server key (or mock client in tests).
    """
    dirty_path = "data/benchmark/dirty_sample.csv"
    prof_res = client.post("/api/profile", json={"filepath": dirty_path})
    session_id = prof_res.json()["session_id"]

    with patch.dict(os.environ, {"ALLOW_SERVER_GEMINI_FALLBACK": "true"}):
        plan_res = client.post(
            "/api/plan",
            json={
                "session_id": session_id,
                "provider": "mock",
            },
        )
        assert plan_res.status_code == 200
        assert "actions" in plan_res.json()


def test_gemini_server_key_fallback_disabled_blocks_anonymous_users(client):
    """
    Verify that when ALLOW_SERVER_GEMINI_FALLBACK=false, requests without api_key
    are rejected with 403 Forbidden.
    """
    dirty_path = "data/benchmark/dirty_sample.csv"
    prof_res = client.post("/api/profile", json={"filepath": dirty_path})
    session_id = prof_res.json()["session_id"]

    with patch.dict(os.environ, {"ALLOW_SERVER_GEMINI_FALLBACK": "false"}):
        plan_res = client.post(
            "/api/plan",
            json={
                "session_id": session_id,
                "provider": "gemini",
                "api_key": None,
            },
        )
        assert plan_res.status_code == 403
        assert "fallback is disabled" in plan_res.json()["detail"].lower()


# ==============================================================================
# 4. GEMINI OVERRIDE BEHAVIOR
# ==============================================================================

def test_gemini_override_does_not_mutate_server_environment(client):
    """
    Verify that supplying a user api_key override in the request NEVER modifies
    the server-side os.environ['GEMINI_API_KEY'].
    """
    original_env_key = os.getenv("GEMINI_API_KEY")
    user_override_key = "AIzaSyUserCustomKeyOverride9999999999"

    dirty_path = "data/benchmark/dirty_sample.csv"
    prof_res = client.post("/api/profile", json={"filepath": dirty_path})
    session_id = prof_res.json()["session_id"]

    client.post(
        "/api/plan",
        json={
            "session_id": session_id,
            "provider": "mock",
            "api_key": user_override_key,
        },
    )

    # Server environment must remain unchanged
    assert os.getenv("GEMINI_API_KEY") == original_env_key


# ==============================================================================
# 5. RATE LIMITING
# ==============================================================================

def test_ai_rate_limiting_burst_per_minute(client):
    """
    Verify that exceeding AI_RATE_LIMIT_PER_MINUTE triggers HTTP 429 Too Many Requests
    with a Retry-After header.
    """
    dirty_path = "data/benchmark/dirty_sample.csv"
    prof_res = client.post("/api/profile", json={"filepath": dirty_path})
    session_id = prof_res.json()["session_id"]

    # Temporarily set burst limit to 3 requests per minute
    with patch.dict(os.environ, {"AI_RATE_LIMIT_PER_MINUTE": "3", "ALLOW_SERVER_GEMINI_FALLBACK": "true"}):
        limiter.reset()

        # Requests 1, 2, 3 succeed
        for _ in range(3):
            res = client.post("/api/plan", json={"session_id": session_id, "provider": "mock"})
            assert res.status_code == 200

        # Request 4 must be rate-limited
        res_blocked = client.post("/api/plan", json={"session_id": session_id, "provider": "mock"})
        assert res_blocked.status_code == 429
        assert "rate limit exceeded" in res_blocked.json()["detail"].lower()
        assert "retry-after" in res_blocked.headers


def test_ai_server_key_hourly_quota_enforced(client):
    """
    Verify that requests relying on the server key are subject to AI_SERVER_KEY_LIMIT_PER_HOUR.
    """
    dirty_path = "data/benchmark/dirty_sample.csv"
    prof_res = client.post("/api/profile", json={"filepath": dirty_path})
    session_id = prof_res.json()["session_id"]

    with patch.dict(os.environ, {"AI_SERVER_KEY_LIMIT_PER_HOUR": "2", "AI_RATE_LIMIT_PER_MINUTE": "100"}):
        limiter.reset()

        # Requests 1 and 2 succeed
        res1 = client.post("/api/plan", json={"session_id": session_id, "provider": "mock"})
        assert res1.status_code == 200
        res2 = client.post("/api/plan", json={"session_id": session_id, "provider": "mock"})
        assert res2.status_code == 200

        # Request 3 without custom key must fail with 429 server quota exceeded
        res3 = client.post("/api/plan", json={"session_id": session_id, "provider": "mock"})
        assert res3.status_code == 429
        assert "quota exceeded" in res3.json()["detail"].lower()


def test_custom_api_key_bypasses_server_key_hourly_quota(client):
    """
    Verify that providing a personal API key allows the user to continue planning
    even if the server-key quota has been reached.
    """
    dirty_path = "data/benchmark/dirty_sample.csv"
    prof_res = client.post("/api/profile", json={"filepath": dirty_path})
    session_id = prof_res.json()["session_id"]

    with patch.dict(os.environ, {"AI_SERVER_KEY_LIMIT_PER_HOUR": "1", "AI_RATE_LIMIT_PER_MINUTE": "100"}):
        limiter.reset()

        # Exhaust server quota (1 request)
        res1 = client.post("/api/plan", json={"session_id": session_id, "provider": "mock"})
        assert res1.status_code == 200

        # Next server-key request fails
        res2 = client.post("/api/plan", json={"session_id": session_id, "provider": "mock"})
        assert res2.status_code == 429

        # Request with user-provided key succeeds!
        res3 = client.post(
            "/api/plan",
            json={
                "session_id": session_id,
                "provider": "mock",
                "api_key": "AIzaSyUserPersonalKey123456789012345",
            },
        )
        assert res3.status_code == 200


# ==============================================================================
# 6. CORS RESTRICTIONS
# ==============================================================================

def test_production_cors_rejects_wildcard():
    """Verify that in production, wildcard origin '*' is strictly prohibited."""
    with pytest.raises(ValueError, match="Wildcard origin '\\*' is strictly prohibited in production"):
        validate_cors_origins(origins_str="*", environment="production")


def test_production_cors_rejects_missing_origins():
    """Verify that in production, omitting ALLOWED_ORIGINS raises a configuration error."""
    with pytest.raises(ValueError, match="ALLOWED_ORIGINS must be explicitly configured"):
        validate_cors_origins(origins_str="", environment="production")


def test_production_cors_accepts_valid_domain_origins():
    """Verify that in production, explicit valid origins are validated and credentials enabled."""
    origins, allow_creds = validate_cors_origins(
        origins_str="https://datarefine.example.com, https://app.example.com/",
        environment="production",
    )
    assert "https://datarefine.example.com" in origins
    assert "https://app.example.com" in origins  # Trailing slash stripped
    assert allow_creds is True


def test_development_cors_allows_permissive_defaults():
    """Verify that in development, default origins remain developer-friendly."""
    origins, allow_creds = validate_cors_origins(origins_str="*", environment="development")
    assert origins == ["*"]
    assert allow_creds is False


# ==============================================================================
# 7. SECURITY RESPONSE HEADERS
# ==============================================================================

def test_security_headers_present_on_all_responses(client):
    """
    Verify that security response headers (CSP, nosniff, DENY, Referrer-Policy)
    are attached to every HTTP response.
    """
    res = client.get("/api/health")
    assert res.status_code == 200

    headers = res.headers
    assert "content-security-policy" in headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("x-frame-options") == "DENY"
    assert headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_strict_transport_security_on_https_requests(client):
    """Verify HSTS header is attached when request is routed via HTTPS / X-Forwarded-Proto."""
    res = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert res.status_code == 200
    assert "strict-transport-security" in res.headers
    assert "max-age=31536000" in res.headers["strict-transport-security"]


# ==============================================================================
# 8. DOCKER HARDENING & NON-ROOT EXECUTION
# ==============================================================================

def test_dockerfile_hardened_non_root_user():
    """Verify that Dockerfile creates and runs as an unprivileged appuser with dynamic PORT."""
    with open("Dockerfile", "r", encoding="utf-8") as f:
        dockerfile = f.read()

    # 1. Non-root user creation
    assert "useradd" in dockerfile and "appuser" in dockerfile
    # 2. USER switch
    assert "USER appuser" in dockerfile
    # 3. Dynamic PORT support
    assert "${PORT:-8000}" in dockerfile
    # 4. Appropriate directory ownership
    assert "chown -R appuser:appuser" in dockerfile or "chown -R appuser:appgroup" in dockerfile


# ==============================================================================
# 9. PRODUCTION CONFIGURATION VALIDATION
# ==============================================================================

def test_production_environment_validation_helpers():
    """Verify that invalid origins with missing schemes are caught."""
    with pytest.raises(ValueError, match="Must specify http:// or https:// scheme"):
        validate_cors_origins(origins_str="datarefine.example.com", environment="production")
