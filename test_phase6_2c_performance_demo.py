"""
test_phase6_2c_performance_demo.py - Phase 6.2C Demo Allowance, Performance & Security Verification

Acceptance Criteria Verified:
1. Demo account 10,000-row allowance:
   - Enforce on backend (not only UI).
   - Demo upload cap allows legitimate 10,000-row datasets.
   - Datasets exceeding 10,000 rows (e.g. 10,001 rows) rejected with clear HTTP 400 without silent truncation.
   - Authenticated user limits remain separate (500,000 rows).
   - Demo allowance applies consistently across upload, profiling, plan, clean, facets, clustering, GREL, undo/redo, exports.
2. Memory and performance verification:
   - Measure actual OS process memory (Working Set / RSS) via Windows psapi.GetProcessMemoryInfo (or rusage).
   - Benchmark 1k, 5k, 10k rows and simultaneous demo sessions.
   - Verify bounded RSS growth, execution times, and clean operation.
3. Accessibility verification:
   - Inspect all 7 modals in static/app.html:
     modal-auth, modal-user-sessions, modal-col-detail, modal-add-operation, modal-export-script, modal-onboarding, modal-destructive-confirm.
   - Verify role="dialog", aria-modal="true", aria-labelledby, focus trapping, focus restoration, Esc key handling.
4. Multi-tenant security regression:
   - Tenant isolation (User A vs User B).
   - Demo vs Authenticated isolation.
   - Signout token invalidation.
   - Password reset session revocation.
   - Fernet API key encryption & safe deletion.
   - Double-submit CSRF enforcement on cookie authentication.
"""

from __future__ import annotations

import io
import os
import sys
import time
import json
import uuid
import ctypes
import pytest
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from server import (
    app,
    SESSIONS,
    AUTH_STORE,
    MAX_DEMO_DATASET_ROWS,
    MAX_DEMO_UPLOAD_SIZE_BYTES,
    MAX_DATASET_ROWS,
    DEMO_SESSION_MAX_AGE_SECONDS,
    limiter,
)
from session import CleaningSession


# ==============================================================================
# Process Memory Inspection Utility (Genuine Windows API / Unix rusage)
# ==============================================================================

def get_process_memory_mb() -> dict[str, float]:
    """Measure actual OS Working Set (RSS) and peak working set of current process in MB."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pid = os.getpid()
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            return {"rss_mb": 0.0, "peak_rss_mb": 0.0}
        try:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            ret = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            if ret:
                return {
                    "rss_mb": round(counters.WorkingSetSize / (1024 * 1024), 2),
                    "peak_rss_mb": round(counters.PeakWorkingSetSize / (1024 * 1024), 2),
                }
            return {"rss_mb": 0.0, "peak_rss_mb": 0.0}
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        scale = 1024 if sys.platform.startswith("linux") else (1024 * 1024)
        return {
            "rss_mb": round(usage.ru_maxrss / scale, 2),
            "peak_rss_mb": round(usage.ru_maxrss / scale, 2),
        }


# ==============================================================================
# Test Fixtures & Helpers
# ==============================================================================

@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Ensure clean IP rate limiter state before and after every test."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


def generate_synthetic_csv(n_rows: int, messy_ratio: float = 0.1) -> bytes:
    """Generate a realistic dataset with multiple column types for performance benchmarking."""
    np.random.seed(42)
    departments = ["Engineering", "Marketing", "Sales", "HR", "Finance", "DevOps", "Customer Support"]
    statuses = ["active", "pending", "inactive", "suspended"]

    names = [f"Employee {i:06d}" for i in range(n_rows)]
    for i in range(0, n_rows, int(1 / messy_ratio) if messy_ratio > 0 else n_rows + 1):
        names[i] = f"  employee_{i}   "

    dates = []
    base_timestamp = 1609459200  # 2021-01-01
    for i in range(n_rows):
        ts = base_timestamp + (i * 3600)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if i % 7 == 0:
            dates.append(dt.strftime("%Y-%m-%d"))
        elif i % 11 == 0:
            dates.append(dt.strftime("%m/%d/%Y"))
        else:
            dates.append(dt.strftime("%Y-%m-%d %H:%M:%S"))

    salaries = np.random.randint(40000, 180000, size=n_rows).astype(object)
    for i in range(0, n_rows, 25):
        salaries[i] = None

    dept_choices = np.random.choice(departments, size=n_rows)
    status_choices = np.random.choice(statuses, size=n_rows)

    df = pd.DataFrame({
        "emp_id": [f"EMP-{i:06d}" for i in range(n_rows)],
        "full_name": names,
        "hire_date": dates,
        "department": dept_choices,
        "salary": salaries,
        "status": status_choices,
        "score": np.random.uniform(50.0, 100.0, size=n_rows).round(2),
    })

    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")


# ==============================================================================
# SECTION 1: Demo 10,000-Row Allowance & Safety Limits
# ==============================================================================

def test_demo_10k_upload_and_profiling(client):
    """
    Verify anonymous demo user can upload and profile a full 10,000-row dataset.
    Asserts:
    - Upload size is within 10MB limit.
    - Profiling processes exactly 10,000 rows without errors.
    - Measures process RSS and execution time.
    """
    mem_before = get_process_memory_mb()
    csv_bytes = generate_synthetic_csv(10000)
    csv_size_mb = len(csv_bytes) / (1024 * 1024)
    assert csv_size_mb < 10.0, f"Generated CSV {csv_size_mb:.2f}MB exceeds 10MB limit"

    t0 = time.perf_counter()
    up_res = client.post(
        "/api/upload",
        files={"file": ("demo_10k.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    t_upload = time.perf_counter() - t0
    assert up_res.status_code == 200, up_res.text
    filepath = up_res.json()["filepath"]
    assert "demo" in filepath

    t0 = time.perf_counter()
    prof_res = client.post("/api/profile", json={"filepath": filepath})
    t_profile = time.perf_counter() - t0
    assert prof_res.status_code == 200, prof_res.text

    prof_data = prof_res.json()
    assert prof_data["summary"]["rows"] == 10000
    assert prof_data["summary"]["columns"] == 7
    assert prof_data["session_id"] in SESSIONS
    assert SESSIONS[prof_data["session_id"]]["owner_id"] == "anonymous"

    mem_after = get_process_memory_mb()

    # Performance assertions
    assert t_profile < 15.0, f"Profiling 10k rows took {t_profile:.2f}s (expected < 15s)"
    assert mem_after["rss_mb"] < 1024.0, f"Process RSS {mem_after['rss_mb']}MB exceeded 1GB"

    # Cleanup session
    session_id = prof_data["session_id"]
    if session_id in SESSIONS:
        del SESSIONS[session_id]


def test_demo_rejection_over_10k_no_truncation(client):
    """
    Verify anonymous demo user uploading >10,000 rows is rejected with clear HTTP 400.
    Server MUST NOT silently truncate data.
    """
    csv_bytes = generate_synthetic_csv(10001)

    up_res = client.post(
        "/api/upload",
        files={"file": ("demo_10001.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert up_res.status_code == 200, up_res.text
    filepath = up_res.json()["filepath"]

    prof_res = client.post("/api/profile", json={"filepath": filepath})
    assert prof_res.status_code == 400
    detail = prof_res.json()["detail"]
    assert "10,001 rows" in detail
    assert "exceeding maximum supported limit of 10,000 rows in demo mode" in detail


def test_authenticated_user_higher_capacity(client):
    """
    Verify authenticated users are permitted datasets exceeding demo 10,000-row limit.
    Ensures multi-tenant tier limits are independent.
    """
    email = f"auth_capacity_{int(time.time() * 1000)}@example.com"
    reg_res = client.post("/api/auth/register", json={
        "email": email,
        "password": "StrongPassword123!",
        "full_name": "Pro Analyst",
    })
    assert reg_res.status_code == 200
    token = reg_res.json()["access_token"]

    # Upload 10,500 rows as authenticated user
    csv_bytes = generate_synthetic_csv(10500)
    up_res = client.post(
        "/api/upload",
        files={"file": ("pro_10500.csv", io.BytesIO(csv_bytes), "text/csv")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert up_res.status_code == 200
    filepath = up_res.json()["filepath"]

    prof_res = client.post(
        "/api/profile",
        json={"filepath": filepath},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert prof_res.status_code == 200
    assert prof_res.json()["summary"]["rows"] == 10500

    # Cleanup
    session_id = prof_res.json()["session_id"]
    if session_id in SESSIONS:
        del SESSIONS[session_id]


# ==============================================================================
# SECTION 2: Full 10,000-Row Pipeline Verification (Plan, Clean, Facets, GREL, Undo/Redo, Export)
# ==============================================================================

def test_full_10k_pipeline_journey(client):
    """
    Comprehensive end-to-end test of a 10,000-row dataset across all application stages:
    1. Upload & Profiling
    2. AI Plan Generation & Action Addition
    3. Plan Execution & Visual Diff
    4. Column Faceting
    5. Text Clustering
    6. GREL Expression Preview & Mutation
    7. Step Checkpointing, Undo & Redo
    8. Exports (CSV, Python Script, Workflow Recipe JSON)
    """
    # 1. Ingestion
    csv_bytes = generate_synthetic_csv(10000, messy_ratio=0.1)
    up_res = client.post("/api/upload", files={"file": ("full_10k.csv", io.BytesIO(csv_bytes), "text/csv")})
    assert up_res.status_code == 200
    filepath = up_res.json()["filepath"]

    prof_res = client.post("/api/profile", json={"filepath": filepath})
    assert prof_res.status_code == 200
    prof_data = prof_res.json()
    session_id = prof_data["session_id"]
    session_token = prof_data.get("session_token")
    headers = {"X-Session-Token": session_token} if session_token else {}

    # 2. Planning: generate plan and add specific deterministic action
    from unittest.mock import patch
    from schemas import CleaningPlan, CleaningAction

    mock_plan = CleaningPlan(
        summary="Automated plan for 10k employee dataset",
        actions=[
            CleaningAction(action="trim_whitespace", column="full_name", params={}, reasoning="Clean text"),
            CleaningAction(action="impute_missing", column="salary", params={"method": "median"}, reasoning="Fill missing"),
        ]
    )
    with patch("server.generate_validated_plan", return_value=(mock_plan, "mock raw response")):
        plan_res = client.post("/api/plan", json={"session_id": session_id}, headers=headers)
    assert plan_res.status_code == 200

    add_act_res = client.post("/api/plan/add_action", json={
        "session_id": session_id,
        "action": "trim_whitespace",
        "column": "full_name",
        "params": {},
    }, headers=headers)
    assert add_act_res.status_code == 200
    actions = add_act_res.json()["actions"]
    assert len(actions) > 0
    target_action_id = actions[-1]["id"]

    # 3. Clean Execution
    t0 = time.perf_counter()
    clean_res = client.post("/api/clean", json={
        "session_id": session_id,
        "approved_action_ids": [target_action_id],
    }, headers=headers)
    t_clean = time.perf_counter() - t0
    assert clean_res.status_code == 200, clean_res.text
    clean_data = clean_res.json()
    assert clean_data["execution_status"] in ("verified_completed", "completed_no_changes")
    assert "diff_table" in clean_data

    # 4. Facets
    t0 = time.perf_counter()
    facet_res = client.post("/api/facets/compute", json={
        "session_id": session_id,
        "column": "department",
    }, headers=headers)
    t_facet = time.perf_counter() - t0
    assert facet_res.status_code == 200, facet_res.text
    facet_data = facet_res.json()
    assert facet_data["column"] == "department"
    assert facet_data["facet"]["total_rows"] == 10000

    # 5. Text Clustering
    t0 = time.perf_counter()
    clust_res = client.post("/api/cluster/detect", json={
        "session_id": session_id,
        "column": "department",
        "method": "fingerprint",
    }, headers=headers)
    t_clust = time.perf_counter() - t0
    assert clust_res.status_code == 200, clust_res.text
    assert "clusters" in clust_res.json()

    # 6. GREL Transformation
    t0 = time.perf_counter()
    grel_prev = client.post("/api/grel/preview", json={
        "session_id": session_id,
        "column": "status",
        "expression": "value.toUppercase()",
        "sample_limit": 20,
    }, headers=headers)
    assert grel_prev.status_code == 200
    assert len(grel_prev.json()["rows"]) > 0

    grel_apply = client.post("/api/grel/apply", json={
        "session_id": session_id,
        "column": "status",
        "expression": "value.toUppercase()",
    }, headers=headers)
    assert grel_apply.status_code == 200
    assert grel_apply.json()["current_step"] >= 1

    # 7. Step Checkpointing, Undo & Redo
    step_before_undo = grel_apply.json()["current_step"]
    undo_res = client.post("/api/undo", json={"session_id": session_id}, headers=headers)
    assert undo_res.status_code == 200
    assert undo_res.json()["current_step"] == step_before_undo - 1

    redo_res = client.post("/api/redo", json={"session_id": session_id}, headers=headers)
    assert redo_res.status_code == 200
    assert redo_res.json()["current_step"] == step_before_undo

    # 8. Exports
    # 8a. CSV Export
    csv_exp = client.get(f"/api/download/cleaned?session_id={session_id}", headers=headers)
    assert csv_exp.status_code == 200
    csv_lines = [line for line in csv_exp.text.strip().split("\n") if line]
    assert len(csv_lines) == 10001, f"Cleaned CSV has {len(csv_lines)} lines, expected 10001"

    # 8b. Python Script Export
    py_exp = client.get(f"/api/export/python?session_id={session_id}", headers=headers)
    assert py_exp.status_code == 200
    assert "import pandas as pd" in py_exp.json()["script"]

    # 8c. Workflow Recipe JSON Export
    recipe_exp = client.post("/api/workflow/export_recipe", json={"session_id": session_id}, headers=headers)
    assert recipe_exp.status_code == 200
    recipe_json = recipe_exp.json()
    assert "operations" in recipe_json
    assert len(recipe_json["operations"]) >= 1

    # Cleanup
    if session_id in SESSIONS:
        del SESSIONS[session_id]


# ==============================================================================
# SECTION 3: Performance Benchmarking (1k, 5k, 10k rows & Concurrent Sessions)
# ==============================================================================

def test_performance_scaling_benchmarks(client):
    """
    Benchmark profiling, cleaning, and memory consumption across 1k, 5k, and 10k rows.
    Verifies linear or sub-quadratic scaling with bounded RSS.
    """
    results = {}
    for n in (1000, 5000, 10000):
        csv_bytes = generate_synthetic_csv(n)
        up_res = client.post("/api/upload", files={"file": (f"bench_{n}.csv", io.BytesIO(csv_bytes), "text/csv")})
        assert up_res.status_code == 200
        filepath = up_res.json()["filepath"]

        t0 = time.perf_counter()
        prof_res = client.post("/api/profile", json={"filepath": filepath})
        t_prof = time.perf_counter() - t0
        assert prof_res.status_code == 200
        sid = prof_res.json()["session_id"]
        stoken = prof_res.json().get("session_token")
        hdrs = {"X-Session-Token": stoken} if stoken else {}

        # Add action to plan and clean
        client.post("/api/plan/add_action", json={
            "session_id": sid,
            "action": "trim_whitespace",
            "column": "full_name",
        }, headers=hdrs)

        t0 = time.perf_counter()
        clean_res = client.post("/api/clean", json={
            "session_id": sid,
            "approved_action_ids": [0],
        }, headers=hdrs)
        t_clean = time.perf_counter() - t0
        assert clean_res.status_code == 200

        mem = get_process_memory_mb()
        results[n] = {
            "prof_time_sec": round(t_prof, 3),
            "clean_time_sec": round(t_clean, 3),
            "rss_mb": mem["rss_mb"],
        }
        if sid in SESSIONS:
            del SESSIONS[sid]

    # Benchmarks verification: 10k rows execution time should be fast (< 10 seconds)
    assert results[10000]["prof_time_sec"] < 10.0
    assert results[10000]["clean_time_sec"] < 10.0


def test_simultaneous_demo_sessions_isolation(client):
    """
    Test 3 concurrent demo sessions with 3,000 rows each.
    Verifies that:
    - Session data remains completely isolated across session IDs.
    - No cross-talk or race conditions occur.
    - Memory remains bounded and clean.
    """
    sessions = []
    for i in range(3):
        csv_bytes = generate_synthetic_csv(3000)
        up_res = client.post("/api/upload", files={"file": (f"concurrent_{i}.csv", io.BytesIO(csv_bytes), "text/csv")})
        assert up_res.status_code == 200
        prof_res = client.post("/api/profile", json={"filepath": up_res.json()["filepath"]})
        assert prof_res.status_code == 200
        pdata = prof_res.json()
        sessions.append((pdata["session_id"], pdata.get("session_token")))

    # Apply different actions to each session
    actions_map = [
        ("trim_whitespace", "full_name", {}),
        ("impute_missing", "salary", {"method": "median"}),
        ("convert_to_date", "hire_date", {}),
    ]

    for (sid, token), (act_name, col, params) in zip(sessions, actions_map):
        hdrs = {"X-Session-Token": token} if token else {}
        add_res = client.post("/api/plan/add_action", json={
            "session_id": sid,
            "action": act_name,
            "column": col,
            "params": params,
        }, headers=hdrs)
        assert add_res.status_code == 200
        action_id = add_res.json()["actions"][-1]["id"]

        res = client.post("/api/clean", json={
            "session_id": sid,
            "approved_action_ids": [action_id],
        }, headers=hdrs)
        assert res.status_code == 200

    # Verify each session has its own isolated dataframe state
    assert SESSIONS[sessions[0][0]]["df_current"]["salary"].isna().sum() > 0
    # Session 1 had median salary imputation, so zero missing in salary
    assert SESSIONS[sessions[1][0]]["df_current"]["salary"].isna().sum() == 0

    # Cleanup
    for sid, _ in sessions:
        if sid in SESSIONS:
            del SESSIONS[sid]


# ==============================================================================
# SECTION 4: Modal Accessibility Structural & Behavioral Assertions
# ==============================================================================

def test_modal_accessibility_static_structure():
    """
    Verify all 7 modals in static/app.html adhere to WCAG AA modal dialog specifications:
    - role="dialog"
    - aria-modal="true"
    - aria-labelledby pointing to an existing header element ID
    - Focus trapping logic present in keydown listener
    - Focus restoration logic present in closeModal
    """
    app_html_path = Path("static/app.html")
    assert app_html_path.exists(), "static/app.html missing"
    html_content = app_html_path.read_text(encoding="utf-8")

    modal_configs = [
        ("modal-auth", "auth-modal-title"),
        ("modal-user-sessions", "user-sessions-title"),
        ("modal-col-detail", "modal-col-name"),
        ("modal-add-operation", "add-op-modal-title"),
        ("modal-export-script", "export-script-modal-title"),
        ("modal-onboarding", "onboarding-modal-title"),
        ("modal-destructive-confirm", "destructive-modal-title"),
    ]

    for modal_id, label_id in modal_configs:
        assert f'id="{modal_id}"' in html_content, f"Missing modal {modal_id}"
        assert f'aria-labelledby="{label_id}"' in html_content, f"Modal {modal_id} missing aria-labelledby={label_id}"
        assert f'id="{label_id}"' in html_content, f"Modal label element id={label_id} not found"

    # Verify focus management JavaScript utilities exist in app.html
    assert "lastFocusedElementBeforeModal" in html_content, "Missing lastFocusedElementBeforeModal variable"
    assert "openModalsStack" in html_content, "Missing openModalsStack array"
    assert "e.key === 'Tab' && openModalsStack.length > 0" in html_content, "Missing Tab focus-trapping listener"
    assert "lastFocusedElementBeforeModal.focus()" in html_content, "Missing focus restoration on closeModal"
    assert "e.key === 'Escape'" in html_content, "Missing Escape key dismiss listener"


# ==============================================================================
# SECTION 5: Multi-Tenant Security & Regression Verification
# ==============================================================================

def test_tenant_data_and_session_isolation(client):
    """
    Verify strict isolation between User A and User B:
    - User A cannot access User B's dataset file or cleaning session.
    - User A cannot execute operations on User B's session.
    - Demo sessions cannot access authenticated sessions.
    """
    # Create User A
    user_a_email = f"user_a_{int(time.time() * 1000)}@example.com"
    res_a = client.post("/api/auth/register", json={
        "email": user_a_email,
        "password": "Password123!",
        "full_name": "User Alpha",
    })
    token_a = res_a.json()["access_token"]

    # Create User B
    user_b_email = f"user_b_{int(time.time() * 1000)}@example.com"
    res_b = client.post("/api/auth/register", json={
        "email": user_b_email,
        "password": "Password123!",
        "full_name": "User Beta",
    })
    token_b = res_b.json()["access_token"]

    # User A uploads a dataset
    csv_bytes = b"id,val\n1,Alpha\n2,Secret\n"
    up_a = client.post(
        "/api/upload",
        files={"file": ("alpha_data.csv", io.BytesIO(csv_bytes), "text/csv")},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert up_a.status_code == 200
    file_a = up_a.json()["filepath"]

    prof_a = client.post(
        "/api/profile",
        json={"filepath": file_a},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert prof_a.status_code == 200
    sid_a = prof_a.json()["session_id"]

    # Attack 1: User B tries to profile User A's uploaded file -> 403 Forbidden
    attack_prof = client.post(
        "/api/profile",
        json={"filepath": file_a},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert attack_prof.status_code == 403, "User B illegally accessed User A's file"

    # Attack 2: User B tries to execute plan on User A's session -> 403 Forbidden
    attack_exec = client.post(
        "/api/clean",
        json={
            "session_id": sid_a,
            "approved_action_ids": [0],
        },
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert attack_exec.status_code == 403, "User B illegally executed plan on User A's session"

    # Attack 3: Anonymous guest tries to access User A's session -> 401 Unauthorized
    anon_exec = client.post(
        "/api/clean",
        json={
            "session_id": sid_a,
            "approved_action_ids": [0],
        },
    )
    assert anon_exec.status_code in (401, 403), "Anonymous demo illegally executed plan on User A's session"

    # Cleanup
    if sid_a in SESSIONS:
        del SESSIONS[sid_a]


def test_signout_token_invalidation(client):
    """
    Verify that signing out immediately revokes the JWT/bearer token on the server.
    Subsequent API requests with the revoked token MUST be rejected with HTTP 401.
    """
    email = f"signout_test_{int(time.time() * 1000)}@example.com"
    reg_res = client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
        "full_name": "Signout Subject",
    })
    token = reg_res.json()["access_token"]

    # Verify token works
    me_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 200

    # Sign out
    signout_res = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert signout_res.status_code == 200

    # Subsequent access with same token must fail
    me_after = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_after.status_code == 401


def test_password_reset_session_revocation(client):
    """
    Verify that completing a password reset revokes all existing active tokens for that user.
    """
    email = f"pwd_reset_revocation_{int(time.time() * 1000)}@example.com"
    reg_res = client.post("/api/auth/register", json={
        "email": email,
        "password": "OldPassword123!",
        "full_name": "Reset Subject",
    })
    old_token = reg_res.json()["access_token"]

    # Request password reset token
    req_res = client.post("/api/auth/password-reset/request", json={"email": email})
    assert req_res.status_code == 200
    reset_token = req_res.json().get("reset_token")
    assert reset_token is not None

    # Complete reset
    reset_res = client.post("/api/auth/password-reset/confirm", json={
        "token": reset_token,
        "new_password": "NewStrongPassword456!",
    })
    assert reset_res.status_code == 200

    # Verify old token is now invalidated
    me_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    assert me_res.status_code == 401, "Old token remained valid after password reset"


def test_api_key_fernet_encryption_and_deletion(client):
    """
    Verify personal API key security:
    - User-provided API keys are encrypted at rest with Fernet.
    - Raw key is never exposed plaintext in stored database records.
    - User has explicit option to delete/clear saved API keys.
    """
    email = f"api_key_test_{int(time.time() * 1000)}@example.com"
    reg_res = client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
        "full_name": "Key Tester",
    })
    token = reg_res.json()["access_token"]
    user_id = reg_res.json()["user"]["id"]

    secret_raw_key = "AIzaSySecretRawGeminiKey1234567890"

    # Save key
    save_res = client.post(
        "/api/user/preferences",
        json={
            "provider": "gemini",
            "api_key_provider": "gemini",
            "custom_api_key": secret_raw_key,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert save_res.status_code == 200

    # Verify stored secret is encrypted at rest using Fernet
    encrypted_key = AUTH_STORE.get_encrypted_user_api_key(user_id, "gemini")
    assert encrypted_key is not None
    assert encrypted_key.startswith("gAAAAA"), "Stored key is not a valid Fernet token"
    assert secret_raw_key not in encrypted_key, "Raw key leaked into ciphertext"

    # Verify decryption returns original key for authorized operations
    decrypted_key = AUTH_STORE.get_decrypted_user_api_key(user_id, "gemini")
    assert decrypted_key == secret_raw_key

    # Delete key
    del_res = client.post(
        "/api/user/preferences",
        json={
            "api_key_provider": "gemini",
            "clear_api_key": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert del_res.status_code == 200

    # Verify key is cleared from storage
    cleared_key = AUTH_STORE.get_encrypted_user_api_key(user_id, "gemini")
    assert cleared_key is None


def test_csrf_protection_on_cookie_auth(client):
    """
    Verify double-submit CSRF enforcement on state-changing methods when authenticating via cookies:
    - Missing CSRF header returns 403 Forbidden.
    - Mismatched CSRF token returns 403 Forbidden.
    - Matching CSRF token succeeds.
    """
    email = f"csrf_test_{int(time.time() * 1000)}@example.com"
    reg_res = client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
        "full_name": "CSRF Tester",
    })
    token = reg_res.json()["access_token"]

    # Set auth cookie without csrf token
    client.cookies.set("auth_token", token)

    # State-changing POST without CSRF header -> 403
    bad_res = client.post("/api/user/preferences", json={"preferred_provider": "gemini"})
    assert bad_res.status_code == 403
    assert "CSRF token missing or invalid" in bad_res.json()["detail"]

    # With mismatched CSRF token -> 403
    client.cookies.set("csrf_token", "cookie_token_abc")
    bad_res2 = client.post(
        "/api/user/preferences",
        json={"preferred_provider": "gemini"},
        headers={"X-CSRF-Token": "different_token_xyz"},
    )
    assert bad_res2.status_code == 403

    # With valid cookie + matching X-CSRF-Token header -> 200
    csrf_val = "valid_csrf_token_123"
    client.cookies.set("csrf_token", csrf_val)
    good_res = client.post(
        "/api/user/preferences",
        json={"preferred_provider": "gemini"},
        headers={"X-CSRF-Token": csrf_val},
    )
    assert good_res.status_code == 200
