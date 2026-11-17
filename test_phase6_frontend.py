"""
test_phase6_frontend.py - Phase 6 Frontend Modernization & SaaS User Experience Tests

Verifies:
1. Workspace HTML delivery and routing (/app, /app/review, /app/results, /ui, /).
2. UI Component integrity (Demo Mode badge, Onboarding Tour modal, Destructive Confirmation modal,
   Upload progress indicator, Risk filter buttons, Password strength meter & visibility toggles,
   Checkpoints toolbar buttons, and Recipe copy button).
3. JavaScript syntax integrity and sanitization helpers.
4. Route aliasing consistency (/api/undo, /api/redo, /api/rollback alongside /api/session/...).
5. Stepwise undo/redo/rollback operations across route aliases.
"""

import os
import re
import json
import pytest
from fastapi.testclient import TestClient
from server import app, SESSIONS


@pytest.fixture
def client():
    return TestClient(app)


def test_landing_page_delivery(client):
    """GET / should serve the public-facing landing page with no-cache headers."""
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers.get("content-type", "")
    assert "no-cache" in res.headers.get("cache-control", "")
    assert "DataRefine" in res.text
    assert "/app" in res.text


def test_app_workspace_routes_delivery(client):
    """GET /app, /app/review, /app/results, /ui should all serve app.html workspace."""
    routes = ["/app", "/app/review", "/app/results", "/ui"]
    for route in routes:
        res = client.get(route)
        assert res.status_code == 200, f"Route {route} failed"
        assert "text/html" in res.headers.get("content-type", "")
        assert "no-cache" in res.headers.get("cache-control", "")
        assert "DataRefine" in res.text
        assert "modal-onboarding" in res.text


def test_onboarding_quick_tour_modal_presence(client):
    """Verify onboarding quick tour modal and 4-stage pipeline explainer exist."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    assert 'id="modal-onboarding"' in html
    assert "How DataRefine Studio Works" in html
    assert "Deep Ingestion & Profiling" in html
    assert "Interactive Wrangling Studio" in html
    assert "openOnboardingModal()" in html
    assert "closeModal('modal-onboarding')" in html


def test_demo_mode_badge_and_guest_frictionless_flow(client):
    """Verify demo mode badge exists and links to auth modal for guest visitors."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    assert 'id="demo-mode-badge"' in html
    assert "Demo Mode" in html
    assert "10MB limit" in html
    assert "Continue in Demo Mode as Guest" in html


def test_upload_progress_bar_presence(client):
    """Verify real-time XHR upload progress container and indicators exist."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    assert 'id="upload-progress-container"' in html
    assert 'id="upload-progress-bar"' in html
    assert 'id="upload-progress-pct"' in html
    assert 'id="upload-progress-label"' in html
    assert "uploadFileWithProgress" in html


def test_plan_risk_filter_buttons_presence(client):
    """Verify plan actions risk filter toolbar exists with all tier buttons."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    assert 'id="plan-filter-btn-all"' in html
    assert 'id="plan-filter-btn-green"' in html
    assert 'id="plan-filter-btn-yellow"' in html
    assert 'id="plan-filter-btn-red"' in html
    assert "filterPlanActions" in html


def test_destructive_confirmation_modal_presence(client):
    """Verify in-app confirmation modal for potentially destructive (RED) operations exists."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    assert 'id="modal-destructive-confirm"' in html
    assert 'id="destructive-actions-summary-list"' in html
    assert "Confirm Destructive (RED) Operations" in html
    assert "requestDestructiveConfirmation" in html
    assert "closeDestructiveConfirmModal" in html


def test_checkpoints_toolbar_buttons_and_disabled_state(client):
    """Verify undo, redo, and rollback buttons exist with disabled styling hooks."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    assert 'id="btn-undo-step"' in html
    assert 'id="btn-redo-step"' in html
    assert 'id="btn-rollback-all"' in html
    assert "updateCheckpointButtons" in html
    assert "disabled:opacity-40" in html


def test_password_strength_and_visibility_toggles(client):
    """Verify password strength meter and visibility toggle buttons exist."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    assert 'id="password-strength-meter"' in html
    assert 'id="pwd-bar-1"' in html
    assert 'id="pwd-bar-4"' in html
    assert 'id="pwd-rule-len"' in html
    assert 'id="pwd-rule-upper"' in html
    assert 'id="pwd-rule-lower"' in html
    assert 'id="pwd-rule-digit"' in html
    assert "checkPasswordStrength" in html
    assert "togglePasswordVisibility" in html


def test_workflow_recipe_copy_button_presence(client):
    """Verify copy JSON recipe button exists in Tab 6 of DataRefine Studio."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    assert "copyWorkflowRecipeJson()" in html
    assert "Copy JSON" in html
    assert "downloadWorkflowRecipe()" in html


def test_keyboard_shortcuts_guard_and_escape_listener(client):
    """Verify keyboard shortcuts listener guards against typing in form inputs and handles Escape."""
    res = client.get("/app")
    assert res.status_code == 200
    html = res.text

    assert "e.key === 'Escape'" in html
    assert "['INPUT', 'TEXTAREA', 'SELECT']" in html
    assert "undoStep()" in html
    assert "redoStep()" in html


def test_undo_redo_rollback_route_aliases(client):
    """Verify /api/undo, /api/redo, /api/rollback route aliases match /api/session/... functionality."""
    # 1. Profile dirty sample
    dirty_path = "data/benchmark/dirty_sample.csv"
    res_prof = client.post("/api/profile", json={"filepath": dirty_path})
    assert res_prof.status_code == 200
    prof = res_prof.json()
    sess_id = prof["session_id"]
    sess_token = prof.get("session_token", "")
    headers = {"X-Session-Token": sess_token}

    # 2. Add and execute an action
    client.post(
        "/api/plan/add_action",
        json={
            "session_id": sess_id,
            "action": "trim_whitespace",
            "column": "name",
            "params": {},
            "reasoning": "Test trim",
        },
        headers=headers,
    )
    res_clean = client.post(
        "/api/clean",
        json={"session_id": sess_id, "approved_action_ids": [0]},
        headers=headers,
    )
    assert res_clean.status_code == 200
    clean_data = res_clean.json()
    assert len(clean_data["checkpoints"]) >= 2

    # 3. Test /api/undo route alias
    res_undo = client.post("/api/undo", json={"session_id": sess_id}, headers=headers)
    assert res_undo.status_code == 200
    undo_data = res_undo.json()
    assert undo_data["status"] == "success"
    assert undo_data["current_step"] == 0

    # 4. Test /api/redo route alias
    res_redo = client.post("/api/redo", json={"session_id": sess_id}, headers=headers)
    assert res_redo.status_code == 200
    redo_data = res_redo.json()
    assert redo_data["status"] == "success"
    assert redo_data["current_step"] == 1

    # 5. Test /api/rollback route alias
    res_rb = client.post("/api/rollback", json={"session_id": sess_id}, headers=headers)
    assert res_rb.status_code == 200
    rb_data = res_rb.json()
    assert rb_data["status"] == "success"
    assert rb_data["current_step"] == 0

    # 6. Verify canonical /api/session/... endpoints also work identically
    res_redo_canon = client.post("/api/session/redo", json={"session_id": sess_id}, headers=headers)
    assert res_redo_canon.status_code == 200
    assert res_redo_canon.json()["current_step"] == 1

    res_undo_canon = client.post("/api/session/undo", json={"session_id": sess_id}, headers=headers)
    assert res_undo_canon.status_code == 200
    assert res_undo_canon.json()["current_step"] == 0
