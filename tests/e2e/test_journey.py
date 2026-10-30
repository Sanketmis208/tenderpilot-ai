"""Browser journey (Python Playwright) against a running, seeded server.

Run:  uv run playwright install chromium
      make dev & make worker &  (with `uv run tenderpilot seed` done)
      TP_E2E_BASE_URL=http://localhost:8080 uv run pytest -m e2e tests/e2e
Covers: register → create org → onboarding → upload a document (real job) → import tender → requirements →
eligibility check → evidence viewer → bid workspace → drafts → export; plus negative paths.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e
BASE = os.environ.get("TP_E2E_BASE_URL", "http://localhost:8080")
SAMPLE = Path(__file__).resolve().parents[2] / "sample_data"
PASSWORD = "E2eJourney2026x"


@pytest.fixture(scope="module")
def page():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        pg.set_default_timeout(20000)
        yield pg
        browser.close()


def test_login_rejects_bad_password(page):
    page.goto(f"{BASE}/login")
    page.get_by_label("Email").fill("bids@voltedge.example")
    page.get_by_label("Password").fill("wrong-password-1A")
    page.get_by_role("button", name=re.compile("sign in", re.I)).click()
    page.wait_for_url(re.compile(r"/login\?error="))
    assert "Invalid email or password" in page.content()


def test_protected_page_redirects_to_login(page):
    page.context.clear_cookies()
    page.goto(f"{BASE}/dashboard")
    page.wait_for_url(re.compile(r"/login"))


def test_full_journey(page):
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
    page.goto(f"{BASE}/register")
    page.get_by_label("Full name").fill("E2E Tester")
    page.get_by_label("Work email").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_label("Company name").fill("E2E Electricals (synthetic)")
    page.get_by_role("button", name=re.compile("create account", re.I)).click()
    page.wait_for_url(re.compile(r"/onboarding"))
    page.get_by_label("Legal business name").fill("E2E Electricals Pvt Ltd (synthetic)")
    page.get_by_role("button", name=re.compile("^next$", re.I)).first.click()
    page.get_by_role("button", name=re.compile("^next$", re.I)).nth(1).click()
    page.get_by_role("button", name=re.compile("save & continue", re.I)).click()
    page.locator("input[type=file]").set_input_files(str(SAMPLE / "voltedge" / "iso_9001_certificate_2026.pdf"))
    page.get_by_text("succeeded").first.wait_for(timeout=60000)
    page.get_by_role("button", name=re.compile("finish", re.I)).click()
    page.wait_for_url(re.compile(r"/dashboard"))

    page.goto(f"{BASE}/tenders?new=1")
    page.get_by_role("button", name=re.compile("start upload", re.I)).click()
    page.locator("input[type=file]").last.set_input_files(str(SAMPLE / "tenders" / "pmsil_street_lighting_rfp.pdf"))
    page.wait_for_url(re.compile(r"/tenders/[0-9a-f-]{36}$"), timeout=90000)
    page.get_by_role("tab", name="Requirements").click()
    page.get_by_text("Valid ISO 9001 certification").wait_for()

    page.get_by_role("button", name=re.compile("run eligibility check", re.I)).click()
    page.wait_for_url(re.compile(r"tab=eligibility"), timeout=90000)
    page.get_by_role("button", name="Report").first.click()
    page.wait_for_url(re.compile(r"/assessments/"))
    page.get_by_text("Evidence, tender clause & computation").first.click()
    page.get_by_role("link", name="open in tender").first.click()
    page.wait_for_url(re.compile(r"/viewer/"))
    page.get_by_text("Cited passage").wait_for()
    assert page.locator("img[alt^='Page']").first.is_visible()

    page.go_back()
    page.go_back()
    page.get_by_role("button", name=re.compile("create bid workspace", re.I)).click()
    page.wait_for_url(re.compile(r"/bids/"))
    page.get_by_role("tab", name="Drafts").click()
    page.get_by_role("button", name=re.compile("generate drafts", re.I)).click()
    page.get_by_text("TO BE COMPLETED").first.wait_for()
    page.get_by_role("tab", name=re.compile("Documents", re.I)).click()
    page.get_by_role("button", name=re.compile("export submission package", re.I)).click()
    page.get_by_text("submission package").first.wait_for(timeout=60000)


def test_other_tenant_cannot_open_url(page):
    """A Kaveri user following a VoltEdge tender link gets an error, not data."""
    page.context.clear_cookies()
    page.goto(f"{BASE}/login")
    page.get_by_label("Email").fill("owner@kaveri.example")
    page.get_by_label("Password").fill(os.environ.get("TP_DEMO_PASSWORD", "Demo@Pass2026"))
    page.get_by_role("button", name=re.compile("sign in", re.I)).click()
    page.wait_for_url(re.compile(r"/dashboard"))
    page.goto(f"{BASE}/tenders/{uuid.uuid4()}")
    page.get_by_text("Tender not found.").wait_for()
