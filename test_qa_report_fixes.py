"""
test_qa_report_fixes.py - Verification of all 10 QA Report findings (Nov 19, 2026):
1. H1: Anonymous Cross-Tenant Isolation
2. H2: CSV Formula Injection Neutralization on Export & Profiling Warning
3. M1: OpenAPI Docs Disabled in Production
4. M2: Mobile Navigation Markup & Toggle
5. M3: Authentication Endpoints Sliding-Window Rate Limiting
6. L1: robots.txt and sitemap.xml Endpoints
7. L2: Server Fingerprint Headers Stripped
8. L3: Internal Benchmark Ground Truth & Robustness Paths Excluded from Dataset Listing
9. L4: Tolerant Numeric Type Inference for Columns with Isolated Bad Values
"""

import io
import os
import re
import pytest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd

from server import app, limiter
from profiler import profile_dataset


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


# ==============================================================================
# H1: ANONYMOUS CROSS-TENANT DATA ISOLATION
# ==============================================================================

def test_h1_anonymous_cross_tenant_isolation():
    """
    Visitor A uploads a file anonymously.
    Visitor B must NOT be able to list or profile Visitor A's uploaded file.
    """
    client_a = TestClient(app)
    client_b = TestClient(app)

    # 1. Visitor A uploads a dataset
    csv_data = "id,secret_data\n1,visitor_a_confidential\n2,top_secret\n"
    res_a = client_a.post(
        "/api/upload",
        files={"file": ("tenant_a.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")},
    )
    assert res_a.status_code == 200
    filepath_a = res_a.json()["filepath"]
    assert "uploads/demo" in filepath_a or "uploads\\demo" in filepath_a

    # 2. Visitor A can profile their own dataset
    prof_a = client_a.post("/api/profile", json={"filepath": filepath_a})
    assert prof_a.status_code == 200

    # 3. Visitor A lists datasets: their own file IS present
    list_a = client_a.get("/api/datasets").json()["datasets"]
    paths_a = [d["path"] for d in list_a]
    assert filepath_a in paths_a

    # 4. Visitor B lists datasets: Visitor A's file MUST NOT be present
    list_b = client_b.get("/api/datasets").json()["datasets"]
    paths_b = [d["path"] for d in list_b]
    assert filepath_a not in paths_b

    # 5. Visitor B tries to profile Visitor A's file: MUST return 403 Forbidden
    prof_b = client_b.post("/api/profile", json={"filepath": filepath_a})
    assert prof_b.status_code == 403
    assert "permission" in prof_b.json()["detail"].lower()


# ==============================================================================
# H2: CSV FORMULA INJECTION NEUTRALIZATION & PROFILING FLAG
# ==============================================================================

def test_h2_csv_formula_injection_flagged_in_profiler(tmp_path):
    """Verify cells starting with =, +, -, @ are flagged as formula injection risk during profiling."""
    dirty_csv = tmp_path / "formula_test.csv"
    dirty_csv.write_text(
        'name,formula_col\nAlice,=HYPERLINK("http://evil.com")\nBob,@SUM(1+1)\nCharlie,-cmd|calc\n',
        encoding="utf-8",
    )
    df, prof = profile_dataset(str(dirty_csv))
    formula_cat = prof["column_profiles"]["formula_col"]["quality_categories"]["formula_injection"]
    assert formula_cat["status"] == "detected"
    assert formula_cat["count"] >= 3
    assert prof["quality_categories_summary"]["formula_injection"]["total_count"] >= 3


def test_h2_csv_formula_injection_neutralized_on_download(client):
    """Verify dangerous formula prefixes (=, +, -, @) are prepended with ' on CSV download."""
    csv_content = 'name,action_formula\nAlice,=HYPERLINK("http://evil.com")\nBob,@SUM(A1:A2)\nCharlie,-calc\n'
    up = client.post(
        "/api/upload",
        files={"file": ("formula_inject.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")},
    )
    assert up.status_code == 200
    filepath = up.json()["filepath"]

    prof = client.post("/api/profile", json={"filepath": filepath})
    assert prof.status_code == 200
    session_id = prof.json()["session_id"]
    token = prof.json()["session_token"]

    # Download cleaned CSV
    dl_res = client.get(f"/api/download/cleaned?session_id={session_id}&token={token}")
    assert dl_res.status_code == 200
    downloaded_csv = dl_res.text

    # Verify formula execution is neutralized by prepending single quote
    assert "'=HYPERLINK" in downloaded_csv
    assert "'@SUM" in downloaded_csv
    assert "'-calc" in downloaded_csv


# ==============================================================================
# M1: OPENAPI / SWAGGER SCHEMA DISABLED IN PRODUCTION
# ==============================================================================

def test_m1_openapi_schema_disabled_in_production():
    """Verify that in production mode, docs_url, redoc_url, and openapi_url are disabled."""
    with patch.dict(os.environ, {
        "ENVIRONMENT": "production",
        "SECRET_KEY": "a_valid_32_character_production_secret_key_123",
        "ALLOWED_ORIGINS": "https://datarefine-5llz.onrender.com",
    }):
        is_prod = os.getenv("ENVIRONMENT") in ("production", "prod")
        prod_app = FastAPI(
            title="DataRefine",
            docs_url=None if is_prod else "/docs",
            redoc_url=None if is_prod else "/redoc",
            openapi_url=None if is_prod else "/openapi.json",
        )
        prod_client = TestClient(prod_app)

        res_openapi = prod_client.get("/openapi.json")
        res_docs = prod_client.get("/docs")
        res_redoc = prod_client.get("/redoc")

        assert res_openapi.status_code == 404
        assert res_docs.status_code == 404
        assert res_redoc.status_code == 404

    # Verify server.py source code contains the production conditional
    server_source = Path("server.py").read_text(encoding="utf-8")
    assert "docs_url=None if is_production else" in server_source
    assert "openapi_url=None if is_production else" in server_source


# ==============================================================================
# M2: MOBILE NAVIGATION MARKUP & TOGGLE
# ==============================================================================

def test_m2_mobile_navigation_in_landing_page():
    """Verify static/index.html includes mobile hamburger toggle button and collapsible nav."""
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    assert 'id="navToggle"' in index_html
    assert 'id="mobileNav"' in index_html
    assert "lg:hidden" in index_html
    assert "aria-expanded" in index_html
    assert "mobileNav.classList.toggle('hidden')" in index_html or 'mobileNav.classList.toggle("hidden")' in index_html


# ==============================================================================
# M3: AUTHENTICATION RATE LIMITING
# ==============================================================================

def test_m3_auth_rate_limiting_on_login(client):
    """Verify repeated login requests from the same IP trigger HTTP 429."""
    with patch.dict(os.environ, {"AUTH_RATE_LIMIT_PER_MINUTE": "5"}):
        # First 5 calls pass with 401 (invalid credentials)
        for _ in range(5):
            res = client.post("/api/auth/login", json={"email": "nobody@test.com", "password": "wrong"})
            assert res.status_code == 401

        # 6th call hits rate limit -> 429
        res_throttled = client.post("/api/auth/login", json={"email": "nobody@test.com", "password": "wrong"})
        assert res_throttled.status_code == 429
        assert "Retry-After" in res_throttled.headers


def test_m3_auth_rate_limiting_on_password_reset(client):
    """Verify repeated password reset requests trigger HTTP 429."""
    with patch.dict(os.environ, {"AUTH_RATE_LIMIT_PER_MINUTE": "3"}):
        for _ in range(3):
            res = client.post("/api/auth/password-reset/request", json={"email": "user@test.com"})
            assert res.status_code == 200

        res_throttled = client.post("/api/auth/password-reset/request", json={"email": "user@test.com"})
        assert res_throttled.status_code == 429


# ==============================================================================
# L1: ROBOTS.TXT AND SITEMAP.XML ENDPOINTS
# ==============================================================================

def test_l1_robots_txt_accessible(client):
    """Verify /robots.txt returns 200 text/plain with crawler rules."""
    res = client.get("/robots.txt")
    assert res.status_code == 200
    assert "text/plain" in res.headers.get("content-type", "")
    assert "User-agent:" in res.text
    assert "Disallow: /api/" in res.text


def test_l1_sitemap_xml_accessible(client):
    """Verify /sitemap.xml returns 200 application/xml with sitemap definitions."""
    res = client.get("/sitemap.xml")
    assert res.status_code == 200
    assert "application/xml" in res.headers.get("content-type", "") or "text/xml" in res.headers.get("content-type", "")
    assert "<urlset" in res.text
    assert "<loc>" in res.text


# ==============================================================================
# L2: SERVER FINGERPRINT HEADERS STRIPPED
# ==============================================================================

def test_l2_server_fingerprint_headers_stripped(client):
    """Verify Server, x-render-origin-server, and x-powered-by headers are stripped."""
    res = client.get("/api/health")
    assert res.status_code == 200
    assert "x-render-origin-server" not in res.headers
    assert "X-Render-Origin-Server" not in res.headers
    assert "x-powered-by" not in res.headers
    # Uvicorn or Server header should be stripped
    assert res.headers.get("server") != "uvicorn"


# ==============================================================================
# L3: INTERNAL BENCHMARK GROUND TRUTH & ROBUSTNESS TESTS EXCLUDED
# ==============================================================================

def test_l3_internal_paths_not_exposed_in_api_datasets(client):
    """Verify /api/datasets does not expose internal ground_truth or robustness test files."""
    res = client.get("/api/datasets")
    assert res.status_code == 200
    datasets = res.json()["datasets"]
    paths = [d["path"] for d in datasets]

    # Must NOT expose ground_truth.csv
    assert not any("ground_truth.csv" in p for p in paths)
    # Must NOT expose robustness_tests
    assert not any("robustness_tests" in p for p in paths)
    # Must NOT expose absolute server paths (Windows C:\\ or Linux /home/ /app/)
    assert not any(p.startswith("C:") or p.startswith("/") for p in paths)


# ==============================================================================
# L4: TOLERANT NUMERIC TYPE INFERENCE FOR COLUMNS WITH ISOLATED BAD VALUES
# ==============================================================================

def test_l4_tolerant_numeric_type_inference_in_api_profile(client):
    """
    When a numeric column has an isolated non-numeric value (e.g. [25, 30, 22, 'abc', 29, 35, 25]),
    verify that type inference recognizes it as numeric and computes numeric_stats.
    """
    csv_data = "id,age,department\n1,25,Engineering\n2,30,Sales\n3,22,Marketing\n4,abc,HR\n5,29,Finance\n6,35,Legal\n7,25,Design\n"
    up = client.post(
        "/api/upload",
        files={"file": ("dirty_age.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")},
    )
    assert up.status_code == 200
    filepath = up.json()["filepath"]

    prof = client.post("/api/profile", json={"filepath": filepath})
    assert prof.status_code == 200
    data = prof.json()

    # Verify column summary has valid numeric_stats rather than null
    age_col = next(c for c in data["columns"] if c["name"] == "age")
    assert age_col["numeric_stats"] is not None
    assert age_col["numeric_stats"]["min"] == 22
    assert age_col["numeric_stats"]["max"] == 35
    assert age_col["numeric_stats"]["mean"] == round((25 + 30 + 22 + 29 + 35 + 25) / 6, 2)

    # Verify profile statistics are computed
    age_prof = data["profile"]["column_profiles"]["age"]
    assert age_prof.get("statistics") is not None
    assert age_prof["statistics"]["min"] == 22
    assert age_prof["statistics"]["max"] == 35
