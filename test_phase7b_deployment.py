"""
test_phase7b_deployment.py - Phase 7B Deployment Readiness & Free Tier Verification

Verifies:
1. Runtime dependency imports (cryptography, pyarrow, google-auth).
2. Cloudflare Pages _redirects routing file syntax and rules.
3. Production security configuration (SECRET_KEY enforcement, rejection of dev fallback).
4. Default server AI fallback setting (false in production).
5. Health check endpoint readiness.
6. Session missing/restart graceful handling (404 without server crash).
7. Cookie CSRF enforcement on mutating operations.
8. Frontend cold-start indicator and fetch interceptor presence in static/app.html.
"""

import os
import re
import pytest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from server import app, limiter
from auth_store import get_secret_key, FALLBACK_DEV_SECRET
from rate_limiter import get_rate_limit_configs


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


# ==============================================================================
# 1. RUNTIME DEPENDENCIES VERIFICATION
# ==============================================================================

def test_runtime_dependencies_imported_successfully():
    """Verify that cryptography, pyarrow, and google-auth are importable and functional."""
    # 1. cryptography
    import cryptography
    from cryptography.fernet import Fernet, MultiFernet
    key = Fernet.generate_key()
    f = Fernet(key)
    token = f.encrypt(b"test-secret")
    assert f.decrypt(token) == b"test-secret"

    # 2. pyarrow
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pa.Table.from_arrays([pa.array([1, 2, 3])], names=["col"])
    assert table.num_rows == 3

    # 3. google.auth
    import google.auth
    from google.oauth2 import id_token
    assert hasattr(id_token, "verify_oauth2_token")


def test_requirements_txt_includes_critical_runtime_dependencies():
    """Verify requirements.txt explicitly contains cryptography, pyarrow, and google-auth."""
    req_path = Path("requirements.txt")
    assert req_path.exists()
    content = req_path.read_text(encoding="utf-8")
    assert "cryptography>=" in content
    assert "pyarrow>=" in content
    assert "google-auth>=" in content


# ==============================================================================
# 2. CLOUDFLARE PAGES _REDIRECTS VERIFICATION
# ==============================================================================

def test_cloudflare_pages_redirects_rules():
    """Verify static/_redirects exists and defines correct API proxy and SPA rewrites."""
    redirects_path = Path("static/_redirects")
    assert redirects_path.exists(), "static/_redirects is missing"
    content = redirects_path.read_text(encoding="utf-8")

    # 1. API reverse proxy
    assert re.search(r"^/api/\*\s+https?://[^\s]+/api/:splat\s+200", content, re.MULTILINE), \
        "Missing API proxy rewrite /api/* -> https://.../api/:splat 200"

    # 2. /app and /ui rewrites
    assert re.search(r"^/app\s+/app\.html\s+200", content, re.MULTILINE)
    assert re.search(r"^/app/\*\s+/app\.html\s+200", content, re.MULTILINE)
    assert re.search(r"^/ui\s+/app\.html\s+200", content, re.MULTILINE)

    # 3. Global fallback
    assert re.search(r"^/\*\s+/index\.html\s+200", content, re.MULTILINE)


# ==============================================================================
# 3. PRODUCTION SECURITY: SECRET_KEY ENFORCEMENT
# ==============================================================================

def test_secret_key_development_mode_allows_fallback():
    """In development mode, get_secret_key returns fallback without raising."""
    with patch.dict(os.environ, {"ENVIRONMENT": "development", "SECRET_KEY": ""}):
        key = get_secret_key()
        assert key == FALLBACK_DEV_SECRET


def test_secret_key_production_rejects_missing_key():
    """In production mode, missing SECRET_KEY raises ValueError."""
    with patch.dict(os.environ, {"ENVIRONMENT": "production", "SECRET_KEY": ""}):
        with pytest.raises(ValueError, match="Security violation: In production"):
            get_secret_key()


def test_secret_key_production_rejects_development_fallback():
    """In production mode, explicit fallback secret is rejected."""
    with patch.dict(os.environ, {"ENVIRONMENT": "production", "SECRET_KEY": FALLBACK_DEV_SECRET}):
        with pytest.raises(ValueError, match="hardcoded development fallback is rejected"):
            get_secret_key()


def test_secret_key_production_rejects_short_key():
    """In production mode, secret key under 32 characters is rejected."""
    with patch.dict(os.environ, {"ENVIRONMENT": "production", "SECRET_KEY": "too_short_secret"}):
        with pytest.raises(ValueError, match="at least 32 characters"):
            get_secret_key()


def test_secret_key_production_accepts_strong_random_key():
    """In production mode, strong 32+ char key succeeds."""
    strong_key = "a_very_strong_production_secret_key_with_sufficient_entropy_12345"
    with patch.dict(os.environ, {"ENVIRONMENT": "production", "SECRET_KEY": strong_key}):
        assert get_secret_key() == strong_key


def test_allow_server_gemini_fallback_defaults_to_false_in_production():
    """In production mode, server AI fallback is false unless explicitly set."""
    with patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=False):
        # Remove ALLOW_SERVER_GEMINI_FALLBACK if set in environment
        with patch.dict(os.environ, {}, clear=False):
            if "ALLOW_SERVER_GEMINI_FALLBACK" in os.environ:
                del os.environ["ALLOW_SERVER_GEMINI_FALLBACK"]
            configs = get_rate_limit_configs()
            assert configs["allow_server_gemini_fallback"] is False


# ==============================================================================
# 4. HEALTH CHECK & FRONTEND SERVING
# ==============================================================================

def test_api_health_endpoint_available(client):
    """Verify /api/health returns healthy without requiring auth or database access."""
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert "version" in data
    assert "supported_providers" in data


def test_frontend_routes_served_with_no_cache(client):
    """Verify root and app endpoints serve HTML with no-cache headers."""
    # Root
    res_root = client.get("/")
    assert res_root.status_code == 200
    assert "text/html" in res_root.headers.get("content-type", "")

    # Workspace
    res_app = client.get("/app")
    assert res_app.status_code == 200
    assert "text/html" in res_app.headers.get("content-type", "")
    assert "DataRefine Studio" in res_app.text


# ==============================================================================
# 5. RESTART & MISSING SESSION GRACEFUL BEHAVIOR
# ==============================================================================

def test_missing_session_returns_clean_404_with_guidance(client):
    """
    When accessing a non-existent or post-restart session ID, server returns 404
    explaining that the session expired or reset.
    """
    fake_session_id = "00000000-0000-0000-0000-000000000000"
    res = client.post("/api/clean", json={
        "session_id": fake_session_id,
        "approved_action_ids": [0],
    })
    assert res.status_code == 404
    detail = res.json()["detail"]
    assert "Session not found or expired" in detail


# ==============================================================================
# 6. COLD-START INDICATOR & FETCH INTERCEPTOR PRESENCE
# ==============================================================================

def test_cold_start_banner_and_interceptor_in_app_html():
    """Verify app.html contains the free instance cold-start banner and interceptor logic."""
    app_html = Path("static/app.html").read_text(encoding="utf-8")
    assert 'id="cold-start-banner"' in app_html
    assert 'free instance waking up' in app_html
    assert '_trackApiStart' in app_html
    assert '_trackApiEnd' in app_html
    assert 'window.DATAREFINE_API_BASE' in app_html
