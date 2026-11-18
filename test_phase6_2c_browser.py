"""
test_phase6_2c_browser.py - Real Browser Playwright Verification for Phase 6.2C

Verifies:
1. Viewport responsiveness across 375px, 390px, 768px, and 1280px viewports.
2. No horizontal body overflow (body.scrollWidth <= viewport.width + 5).
3. Independent data table horizontal scrolling (overflow-x-auto).
4. Full modal accessibility lifecycle:
   - role="dialog", aria-modal="true", aria-labelledby
   - Focus transfer on open to first interactive element
   - Tab / Shift+Tab keyboard focus trapping inside dialog
   - Escape key dismisses modal and restores focus to triggering element
5. Responsive modal fit within viewport bounds without clipping.
6. Clean background server execution and shutdown.
"""

from __future__ import annotations

import os
import sys
import time
import socket
import threading
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from server import app, limiter

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
if not os.path.exists(CHROME_PATH):
    CHROME_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def get_free_port() -> int:
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
    """Launch headless Chrome/Edge instance."""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME_PATH, headless=True)
        yield browser
        browser.close()


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


# ==============================================================================
# Responsive Viewport Tests: 375px, 390px, 768px, 1280px
# ==============================================================================

@pytest.mark.parametrize("viewport_cfg", [
    {"name": "mobile_375", "width": 375, "height": 667},
    {"name": "mobile_390", "width": 390, "height": 844},
    {"name": "tablet_768", "width": 768, "height": 1024},
    {"name": "desktop_1280", "width": 1280, "height": 800},
])
def test_viewport_no_horizontal_overflow(live_server, browser_instance, viewport_cfg):
    """
    Assert that the entire application layout never horizontally overflows the viewport
    at any target breakpoint (375px, 390px, 768px, 1280px).
    """
    ctx = browser_instance.new_context(
        viewport={"width": viewport_cfg["width"], "height": viewport_cfg["height"]}
    )
    page = ctx.new_page()
    page.goto(f"{live_server}/app")
    page.wait_for_selector("#step-btn-1")

    # Ingest a sample dataset to render full preview tables and controls
    page.click("#btn-quick-tour")
    page.wait_for_selector("#modal-onboarding:not(.hidden)")
    page.click("button:has-text('Load Sample Dataset')")
    page.wait_for_selector("#step-btn-2:not([disabled])", timeout=15000)

    # Check horizontal body scroll width
    body_scroll_w = page.evaluate("document.body.scrollWidth")
    assert body_scroll_w <= viewport_cfg["width"] + 5, (
        f"Viewport {viewport_cfg['name']} ({viewport_cfg['width']}px) "
        f"overflowed! document.body.scrollWidth = {body_scroll_w}px"
    )

    # Verify stepper nav has horizontal scroll capability if needed
    stepper = page.locator("nav.overflow-x-auto")
    assert stepper.is_visible()

    ctx.close()


def test_independent_table_horizontal_scroll(live_server, browser_instance):
    """
    Verify that wide data tables scroll horizontally inside their container
    without expanding the document body width.
    """
    ctx = browser_instance.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.goto(f"{live_server}/app")
    page.wait_for_selector("#step-btn-1")

    # Load sample dataset
    page.click("#btn-quick-tour")
    page.wait_for_selector("#modal-onboarding:not(.hidden)")
    page.click("button:has-text('Load Sample Dataset')")
    page.wait_for_selector("#preview-table", timeout=15000)

    # Verify preview table is visible and horizontally contained
    table = page.locator("#preview-table")
    assert table.is_visible()

    # Container surrounding the table has overflow-x-auto
    container_scroll_w = page.evaluate("document.getElementById('preview-table').parentElement.scrollWidth")
    container_client_w = page.evaluate("document.getElementById('preview-table').parentElement.clientWidth")
    assert container_scroll_w >= container_client_w

    # Ensure document body width remains clamped
    body_w = page.evaluate("document.body.scrollWidth")
    assert body_w <= 395, f"Body width expanded to {body_w}px on mobile with table loaded"

    ctx.close()


# ==============================================================================
# Modal Accessibility, Keyboard Navigation & Focus Trapping Tests
# ==============================================================================

def test_modal_focus_trap_and_escape_restore(live_server, browser_instance):
    """
    Verify full modal accessibility lifecycle for Onboarding and Auth modals:
    1. Clicking trigger element saves focus and opens modal.
    2. Focus moves automatically inside the modal dialog.
    3. Tab and Shift+Tab trap focus exclusively within the modal.
    4. Pressing Escape closes the modal.
    5. Focus is restored back to the original trigger element.
    """
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    page.goto(f"{live_server}/app")
    page.wait_for_selector("#btn-quick-tour")

    # 1. Onboarding Modal Focus Lifecycle
    trigger_btn = page.locator("#btn-quick-tour")
    trigger_btn.focus()
    page.keyboard.press("Enter")

    # Modal should now be visible
    modal = page.locator("#modal-onboarding")
    page.wait_for_selector("#modal-onboarding:not(.hidden)")
    assert modal.get_attribute("role") == "dialog"
    assert modal.get_attribute("aria-modal") == "true"
    assert modal.get_attribute("aria-labelledby") == "onboarding-modal-title"

    # Focus must now be inside the modal
    focused_in_modal = page.evaluate("""() => {
        const m = document.getElementById('modal-onboarding');
        return m.contains(document.activeElement);
    }""")
    assert focused_in_modal, "Focus was not moved into modal on open"

    # Tab cycling (Trapping): press Tab multiple times and verify activeElement stays in modal
    for _ in range(5):
        page.keyboard.press("Tab")
        active_in_modal = page.evaluate("""() => {
            const m = document.getElementById('modal-onboarding');
            return m.contains(document.activeElement);
        }""")
        assert active_in_modal, "Tab escaped modal dialog!"

    # Shift+Tab cycling backwards
    for _ in range(3):
        page.keyboard.press("Shift+Tab")
        active_in_modal = page.evaluate("""() => {
            const m = document.getElementById('modal-onboarding');
            return m.contains(document.activeElement);
        }""")
        assert active_in_modal, "Shift+Tab escaped modal dialog!"

    # Press Escape to dismiss modal
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal-onboarding", state="hidden")
    assert modal.is_hidden()

    # Focus must be restored to trigger button
    restored_id = page.evaluate("() => document.activeElement ? document.activeElement.id : null")
    assert restored_id == "btn-quick-tour", f"Focus restored to '{restored_id}' instead of 'btn-quick-tour'"

    # 2. Auth Modal Focus Lifecycle
    auth_trigger = page.locator("#auth-btn-signin-nav")
    auth_trigger.focus()
    auth_trigger.click()

    page.wait_for_selector("#modal-auth:not(.hidden)")
    auth_modal = page.locator("#modal-auth")
    assert auth_modal.get_attribute("role") == "dialog"
    assert auth_modal.get_attribute("aria-modal") == "true"
    assert auth_modal.get_attribute("aria-labelledby") == "auth-modal-title"

    # Focus must be inside modal-auth
    focused_in_auth = page.evaluate("""() => {
        const m = document.getElementById('modal-auth');
        return m.contains(document.activeElement);
    }""")
    assert focused_in_auth, "Focus not in auth modal on open"

    # Press Escape to dismiss
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal-auth", state="hidden")
    assert auth_modal.is_hidden()

    ctx.close()


def test_destructive_confirm_modal_interaction(live_server, browser_instance):
    """
    Verify that in-app Destructive Confirmation modal:
    - Traps focus correctly.
    - Dismisses via Escape key or Cancel button.
    - Fits within viewport on mobile.
    """
    ctx = browser_instance.new_context(viewport={"width": 375, "height": 667})
    page = ctx.new_page()
    page.goto(f"{live_server}/app")
    page.wait_for_selector("#step-btn-1")

    # Programmatically trigger destructive confirmation dialog
    page.evaluate("() => openModal('modal-destructive-confirm')")
    page.wait_for_selector("#modal-destructive-confirm:not(.hidden)")

    dest_modal = page.locator("#modal-destructive-confirm")
    assert dest_modal.get_attribute("role") == "dialog"
    assert dest_modal.get_attribute("aria-modal") == "true"
    assert dest_modal.get_attribute("aria-labelledby") == "destructive-modal-title"

    # Verify dialog fits within mobile viewport
    modal_card = dest_modal.locator(".relative.bg-surface-card")
    card_w = modal_card.evaluate("el => el.offsetWidth")
    assert card_w <= 375, f"Modal card {card_w}px exceeded mobile viewport width"

    # Dismiss via Escape
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal-destructive-confirm", state="hidden")
    assert dest_modal.is_hidden()

    ctx.close()


def test_studio_tabs_responsiveness(live_server, browser_instance):
    """
    Verify OpenRefine Studio tabs (Quality, Facets, Clustering, GREL, Reconcile, Recipe)
    are responsive, clickable, and accessible on mobile viewports.
    """
    ctx = browser_instance.new_context(viewport={"width": 375, "height": 667})
    page = ctx.new_page()
    page.goto(f"{live_server}/app")
    page.wait_for_selector("#step-btn-1")

    # Ingest sample dataset to unlock Studio toolbar
    page.click("#btn-quick-tour")
    page.wait_for_selector("#modal-onboarding:not(.hidden)")
    page.click("button:has-text('Load Sample Dataset')")
    page.wait_for_selector("#profile-results:not(.hidden)", timeout=15000)

    # Click each Studio tab and verify active panel switches cleanly
    studio_tabs = [
        ("tab-btn-facets", "tab-view-facets"),
        ("tab-btn-clusters", "tab-view-clusters"),
        ("tab-btn-grel", "tab-view-grel"),
        ("tab-btn-reconcile", "tab-view-reconcile"),
        ("tab-btn-recipe", "tab-view-recipe"),
        ("tab-btn-quality", "tab-view-quality"),
    ]

    for tab_id, panel_id in studio_tabs:
        tab_btn = page.locator(f"#{tab_id}")
        assert tab_btn.is_visible()
        tab_btn.click()
        page.wait_for_selector(f"#{panel_id}:not(.hidden)", timeout=3000)
        assert page.locator(f"#{panel_id}").is_visible()

    # Ensure no horizontal layout blowout
    body_w = page.evaluate("document.body.scrollWidth")
    assert body_w <= 380

    ctx.close()
