"""
test_phase6_1_browser.py - Real Browser Automated Verification via Playwright & Headless Chrome

Covers:
Journey A: Guest User Ingestion & Profiling (Landing page, Demo Mode, Sample Dataset Ingestion, Metrics)
Journey B: AI Cleaning Planner & Risk Approval (Filter Green/Yellow/Red, Destructive Modal, Execution, Diff)
Journey C: OpenRefine Interactive Wrangling (Facets, Clustering, GREL Preview, Reconciliation, Recipe Replay)
Journey D: Stepwise History (Undo, Redo, Rollback, Checkpoint Button States)
Journey E: Multi-Tenant Authentication (Registration, Session State, Sign Out, Guest Migration)
Journey F: Responsive Layout Verification (Desktop 1280px, Tablet 768px, Mobile 375px)
"""

import os
import sys
import time
import socket
import threading
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from server import app, SESSIONS, limiter

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
if not os.path.exists(CHROME_PATH):
    CHROME_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Start FastAPI server in a background thread on an ephemeral port."""
    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server ready
    timeout = 10
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.1)

    yield base_url


@pytest.fixture(scope="module")
def browser_instance():
    """Launch headless Chrome instance."""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME_PATH, headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser_instance):
    """Provide a fresh browser context and page per test."""
    context = browser_instance.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-US"
    )
    pg = context.new_page()
    # Reset IP rate limiter to prevent throttle during tests
    limiter.reset()
    yield pg
    context.close()


def test_journey_a_guest_user_ingestion(live_server, page):
    """Journey A: Guest user explores landing page, enters demo mode, loads sample dataset and verifies profiling."""
    # 1. Landing page
    page.goto(f"{live_server}/")
    assert "DataRefine" in page.title()
    assert page.locator("text=Launch Studio").first.is_visible()

    # 2. Workspace
    page.goto(f"{live_server}/app")
    assert "DataRefine Studio" in page.title()

    # Verify Demo Mode badge is visible for guest
    demo_badge = page.locator("#demo-mode-badge")
    assert demo_badge.is_visible()
    assert "Demo Mode" in demo_badge.inner_text()

    # Open Quick Tour onboarding modal
    page.click("#btn-quick-tour")
    onboarding_modal = page.locator("#modal-onboarding")
    assert onboarding_modal.is_visible()
    assert "How DataRefine Studio Works" in page.locator("#onboarding-modal-title").inner_text()

    # Close onboarding modal
    page.click("button:has-text('Got It, Let')")
    assert not onboarding_modal.is_visible()

    # Load demonstration dataset
    page.evaluate("selectSampleByName('Messy_Employee_dataset.csv')")
    page.wait_for_selector("#profile-results:not(.hidden)", timeout=15000)

    # Verify metrics populated
    rows_text = page.locator("#stat-rows").inner_text()
    cols_text = page.locator("#stat-cols").inner_text()
    health_text = page.locator("#profile-health-score").inner_text()
    assert int(rows_text.replace(",", "")) > 0
    assert int(cols_text) > 0
    assert "%" in health_text

    # Verify Preview table
    assert page.locator("#preview-table-header th").count() > 1
    assert page.locator("#preview-table-body tr").count() > 0


def test_journey_b_cleaning_planner_and_risk_approval(live_server, page):
    """Journey B: Cleaning plan generation, risk filtering (Green/Yellow/Red), destructive modal, execution, and diff."""
    page.goto(f"{live_server}/app")
    page.evaluate("selectSampleByName('dirty_sample.csv')")
    page.wait_for_selector("#profile-results:not(.hidden)", timeout=15000)

    # Go to Step 2 (Review & Clean)
    page.click("#step-btn-2")
    page.wait_for_selector("#step-2:not(.hidden)", timeout=5000)

    # Synthesize plan
    page.evaluate("generateCleaningPlan()")
    page.wait_for_selector("#actions-list > div", timeout=15000)

    # Test Risk Filtering Buttons
    page.click("#plan-filter-btn-green")
    # All visible action cards should be green
    visible_cards = page.locator("#actions-list > div[data-risk]:visible")
    for i in range(visible_cards.count()):
        assert visible_cards.nth(i).get_attribute("data-risk") == "green"

    page.click("#plan-filter-btn-all")
    assert page.locator("#actions-list > div[data-risk]:visible").count() >= visible_cards.count()

    # Test Destructive (RED) Action in-app confirmation modal
    # Add a synthetic destructive red action to test the modal
    page.evaluate("""
        currentPlan.actions.push({
            action: 'remove_column',
            column: 'income',
            risk_level: 'red',
            risk_label: 'Potentially Destructive',
            reasoning: 'Test remove income column'
        });
        renderPlanView(currentPlan);
    """)

    # Check the red action checkbox
    last_idx = page.locator("#actions-list input[type='checkbox']").count() - 1
    page.locator(f"#action-chk-{last_idx}").check()

    # Click Execute button to trigger destructive modal
    page.click("#btn-execute-clean")
    destructive_modal = page.locator("#modal-destructive-confirm")
    assert destructive_modal.is_visible()
    assert "Confirm Destructive (RED) Operations" in destructive_modal.inner_text()
    assert "income" in destructive_modal.inner_text()
    assert "Remove Column" in destructive_modal.inner_text()

    # Affirm execution via in-app modal button
    page.click("button:has-text('I Understand, Execute Operations')")
    page.wait_for_selector("#step-3:not(.hidden)", timeout=15000)

    # Inspect Visual Diff
    assert page.locator("#diff-rows-delta").is_visible()
    assert page.locator("#diff-table-body tr").count() > 0


def test_journey_c_openrefine_wrangling_studio(live_server, page):
    """Journey C: OpenRefine Facets, Clustering, GREL, Reconciliation, and Recipe Replay."""
    page.goto(f"{live_server}/app")
    page.evaluate("selectSampleByName('dirty_sample.csv')")
    page.wait_for_selector("#profile-results:not(.hidden)", timeout=15000)

    # 1. Facets tab
    page.click("#tab-btn-facets")
    page.wait_for_selector("#tab-view-facets:not(.hidden)", timeout=5000)
    page.evaluate("computeCurrentFacet()")
    page.wait_for_selector("#facet-choices-list > div", timeout=5000)
    assert page.locator("#facet-choices-list > div").count() > 0

    # 2. Clustering Studio
    page.click("#tab-btn-clusters")
    page.wait_for_selector("#tab-view-clusters:not(.hidden)", timeout=5000)
    page.select_option("#cluster-column-select", value="status")
    page.click("#btn-detect-clusters")
    page.wait_for_selector("#cluster-results-header:not(.hidden)", timeout=10000)
    assert page.locator("#cluster-count-label").is_visible()
    # Test high-confidence cluster selection button
    page.click("#btn-cluster-high-conf")

    # 3. GREL Expression Engine
    page.click("#tab-btn-grel")
    page.wait_for_selector("#tab-view-grel:not(.hidden)", timeout=5000)
    page.fill("#grel-expression-input", "value.trim()")
    page.click("#btn-preview-grel")
    page.wait_for_selector("#grel-preview-tbody tr", timeout=5000)
    assert page.locator("#grel-preview-tbody tr").count() > 0
    # Test friendly syntax error guidance
    page.fill("#grel-expression-input", "invalid syntax (((")
    page.click("#btn-preview-grel")
    page.wait_for_selector("#grel-preview-tbody:has-text('Expression Error')", timeout=5000)
    assert "Hint: Supported functions:" in page.locator("#grel-preview-tbody").inner_text()

    # 4. Entity Reconciliation
    page.click("#tab-btn-reconcile")
    page.wait_for_selector("#tab-view-reconcile:not(.hidden)", timeout=5000)
    assert page.locator("#reconcile-threshold-select").is_visible()
    page.select_option("#reconcile-threshold-select", value="0.75")
    page.click("#btn-run-reconcile")
    page.wait_for_selector("#reconcile-output-wrap:not(.hidden)", timeout=10000)
    assert page.locator("#reconcile-summary-text").is_visible()

    # 5. Workflow Recipes
    page.click("#tab-btn-recipe")
    page.wait_for_selector("#tab-view-recipe:not(.hidden)", timeout=5000)
    page.evaluate("loadWorkflowHistory()")
    # Test client-side recipe validation
    page.fill("#recipe-replay-input", '{"invalid": "no operations"}')
    page.click("#btn-replay-recipe")
    # Toast error shown
    page.wait_for_selector("#toast:has-text('Invalid recipe format')", timeout=5000)


def test_journey_d_stepwise_history_undo_redo_rollback(live_server, page):
    """Journey D: Stepwise checkpoints, Undo, Redo, Rollback, and dynamic button disabling."""
    page.goto(f"{live_server}/app")
    page.evaluate("selectSampleByName('dirty_sample.csv')")
    page.wait_for_selector("#profile-results:not(.hidden)", timeout=15000)

    # Step 2 -> Plan -> Clean
    page.click("#step-btn-2")
    page.evaluate("generateCleaningPlan()")
    page.wait_for_selector("#actions-list > div", timeout=15000)
    page.click("#btn-execute-clean")
    page.wait_for_selector("#step-3:not(.hidden)", timeout=15000)

    # Undo
    undo_btn = page.locator("#btn-undo-step")
    redo_btn = page.locator("#btn-redo-step")
    rollback_btn = page.locator("#btn-rollback-all")

    # Undo should be enabled, Redo disabled initially at latest step
    assert not undo_btn.is_disabled()
    assert redo_btn.is_disabled()
    assert not rollback_btn.is_disabled()

    # Execute Undo
    undo_btn.click()
    page.wait_for_timeout(1000)
    # After undoing a step, Redo should become enabled
    assert not redo_btn.is_disabled()

    # Execute Redo
    redo_btn.click()
    page.wait_for_timeout(1000)
    assert not undo_btn.is_disabled()
    assert redo_btn.is_disabled()

    # Execute Rollback (accept native confirm prompt)
    page.on("dialog", lambda dialog: dialog.accept())
    rollback_btn.click()
    page.wait_for_timeout(1000)
    assert undo_btn.is_disabled()
    assert not redo_btn.is_disabled()


def test_journey_e_authentication_and_session_isolation(live_server, page):
    """Journey E: User Registration, Login state, Guest migration, Sign out."""
    page.goto(f"{live_server}/app")
    assert page.locator("#demo-mode-badge").is_visible()

    # Open Auth Modal
    page.click("#auth-btn-signin-nav")
    auth_modal = page.locator("#modal-auth")
    assert auth_modal.is_visible()

    # Switch to Register tab
    page.click("#auth-tab-btn-register")
    assert page.locator("#auth-form-register").is_visible()

    # Password strength meter test
    pwd_input = page.locator("#auth-register-password")
    pwd_input.fill("weak")
    page.wait_for_timeout(200)
    assert page.locator("#pwd-bar-1").get_attribute("class").find("bg-rose-500") != -1

    pwd_input.fill("StrongP@ssw0rd123!")
    page.wait_for_timeout(200)
    assert page.locator("#pwd-bar-4").get_attribute("class").find("bg-emerald-500") != -1

    # Fill registration
    unique_email = f"frontend_test_{int(time.time())}@example.com"
    page.fill("#auth-register-name", "Test Architect")
    page.fill("#auth-register-email", unique_email)
    page.fill("#auth-register-password", "StrongP@ssw0rd123!")
    page.click("#auth-btn-register-submit")

    # Wait for logged in state
    page.wait_for_selector("#auth-user-section:not(.hidden)", timeout=10000)
    assert page.locator("#user-display-email").inner_text() == unique_email
    assert not page.locator("#demo-mode-badge").is_visible()

    # Sign out
    page.click("button[title='Sign out']")
    page.wait_for_selector("#auth-guest-section:not(.hidden)", timeout=5000)
    assert page.locator("#demo-mode-badge").is_visible()


def test_journey_f_responsive_layouts(live_server, browser_instance):
    """Journey F: Responsive layout verification across Desktop (1280px), Tablet (768px), and Mobile (375px)."""
    # 1. Desktop 1280x800
    desktop_ctx = browser_instance.new_context(viewport={"width": 1280, "height": 800})
    p_desktop = desktop_ctx.new_page()
    p_desktop.goto(f"{live_server}/app")
    p_desktop.wait_for_selector("#step-btn-1")
    assert p_desktop.locator("#step-btn-1").is_visible()
    desktop_ctx.close()

    # 2. Tablet 768x1024
    tablet_ctx = browser_instance.new_context(viewport={"width": 768, "height": 1024})
    p_tablet = tablet_ctx.new_page()
    p_tablet.goto(f"{live_server}/app")
    p_tablet.wait_for_selector("#step-btn-1")
    assert p_tablet.locator("#step-btn-1").is_visible()
    tablet_ctx.close()

    # 3. Mobile 375x667 (iPhone SE width)
    mobile_ctx = browser_instance.new_context(viewport={"width": 375, "height": 667})
    p_mobile = mobile_ctx.new_page()
    p_mobile.goto(f"{live_server}/app")
    p_mobile.wait_for_selector("#step-btn-1")

    # Verify stepper is cleanly accessible and does not cause horizontal layout overflow
    stepper_nav = p_mobile.locator("nav.overflow-x-auto")
    assert stepper_nav.is_visible()

    # Verify body scroll width does not wildly blow out viewport
    body_scroll_w = p_mobile.evaluate("document.body.scrollWidth")
    assert body_scroll_w <= 390, f"Mobile body width {body_scroll_w} exceeded acceptable viewport bounds"
    mobile_ctx.close()
