"""Shared fixtures. Every test runs against a throwaway SQLite database and the
mock lookup provider, so the suite never touches bcassessment.ca."""

from __future__ import annotations

import pytest

from app.config import settings
from app.db import connection
from app.services import stats
from app.services.browser import BrowserHealth, browser_manager


@pytest.fixture(autouse=True)
def fresh_browser_health():
    """`browser_manager` is a module-level singleton, so failure counts recorded
    by one test would otherwise leak into the next one's `/health`."""
    browser_manager._health = BrowserHealth()
    browser_manager._restart_pending = False
    browser_manager._restart_reason = None
    yield


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point the service at an empty database for the duration of one test."""
    db_file = tmp_path / "test_bc_assessment.db"
    monkeypatch.setattr(settings, "database_path", str(db_file))

    connection.reset_for_tests()
    connection.init_db(db_file)
    yield db_file
    connection.reset_for_tests()


@pytest.fixture(autouse=True)
def quiet_alerts(monkeypatch):
    """Telegram off by default, and alert de-duplication reset between tests."""
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_ids", "")
    stats.reset_alert_state_for_tests()
    yield
    stats.reset_alert_state_for_tests()


@pytest.fixture
def mock_provider(monkeypatch):
    monkeypatch.setattr(settings, "lookup_provider", "mock")
    monkeypatch.setattr(settings, "scrape_warmup_on_start", False)
    return settings


@pytest.fixture
def client(mock_provider):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


PROPERTY_HTML = """
<html><body>
  <h1>7009 TISDALL ST, VANCOUVER</h1>
  <div class="assessment"><span>Total value</span><span>$817,000</span></div>
  <ul>
    <li><span>Year built</span><span>1989</span></li>
    <li><span>Bedrooms</span><span>2</span></li>
    <li><span>Baths</span><span>2</span></li>
    <li><span>Strata area</span><span>1100</span></li>
    <li><span>Building storeys</span><span>12</span></li>
  </ul>
</body></html>
"""

NO_VALUE_HTML = """
<html><body>
  <h1>Search BC Assessment</h1>
  <input id="rsbSearch" />
</body></html>
"""

BLOCKED_HTML = """
<html><body>
  <h1>Access Denied</h1>
  <p>You do not have permission to access this resource.</p>
</body></html>
"""

CAPTCHA_HTML = """
<html><body>
  <h2>Attention Required!</h2>
  <div>Please complete the captcha to continue.</div>
</body></html>
"""
