"""Playwright browser lifecycle: one browser process, a fresh context per lookup.

The previous implementation reused a single global Page for the lifetime of the
process. That page kept the previous property's DOM, kept BC Assessment's
cookies, and grew Chromium's memory until the site started serving a challenge —
which is why a 2-hourly systemd restart appeared to "fix" the service.

Here the expensive part (the browser process) is reused, while the stateful part
(the context and page) is rebuilt for every lookup and always torn down. A fresh
context cannot carry stale markup or cookies into the next request.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from app.config import settings
from app.schemas import FailureType

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
_BLOCKED_URL_RE = re.compile(
    r"(google-analytics|googletagmanager|doubleclick|facebook|hotjar|clarity)",
    re.I,
)


async def _route_handler(route: Route) -> None:
    request = route.request
    try:
        if request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
        if _BLOCKED_URL_RE.search(request.url):
            await route.abort()
            return
        await route.continue_()
    except Exception:
        # The context can close mid-flight while routes are still resolving;
        # a failed abort/continue here must never surface as a lookup error.
        pass


@dataclass
class BrowserHealth:
    browser_connected: bool = False
    consecutive_failures: int = 0
    lookups_since_browser_launch: int = 0
    total_lookups: int = 0
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    last_failure_type: Optional[str] = None
    browser_launched_at: Optional[str] = None
    restarts: int = 0

    def as_dict(self) -> dict:
        return {
            "browser_connected": self.browser_connected,
            "consecutive_failures": self.consecutive_failures,
            "lookups_since_browser_launch": self.lookups_since_browser_launch,
            "total_lookups": self.total_lookups,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_failure_type": self.last_failure_type,
            "browser_launched_at": self.browser_launched_at,
            "browser_restarts": self.restarts,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BrowserManager:
    """Owns the Playwright browser and hands out clean, disposable contexts."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()
        self._health = BrowserHealth()
        self._restart_pending = False
        self._restart_reason: Optional[str] = None

    # ---- health -----------------------------------------------------------

    @property
    def health(self) -> BrowserHealth:
        self._health.browser_connected = (
            self._browser is not None and self._browser.is_connected()
        )
        return self._health

    @property
    def is_healthy(self) -> bool:
        return self._health.consecutive_failures < settings.unhealthy_after_failures

    def record_success(self) -> None:
        self._health.consecutive_failures = 0
        self._health.last_success_at = _now_iso()
        self._health.total_lookups += 1
        self._health.lookups_since_browser_launch += 1

    def record_failure(self, failure: FailureType) -> None:
        self._health.last_failure_at = _now_iso()
        self._health.last_failure_type = failure.value
        # The lookup still used the browser, so it counts toward the recycle
        # budget regardless of why it failed.
        self._health.total_lookups += 1
        self._health.lookups_since_browser_launch += 1

        # A genuine NOT_FOUND says the caller's address does not exist, not that
        # the scraper is sick. Counting it here drove /health to 503 and forced a
        # Chromium relaunch on ordinary traffic — a run of unknown addresses is
        # normal, and a monitor reacting to that 503 would restart the service,
        # reintroducing the restart loop this work removed.
        if not failure.is_scraper_fault:
            return

        self._health.consecutive_failures += 1
        if self._health.consecutive_failures >= settings.browser_restart_after_failures:
            self.request_restart(
                f"{self._health.consecutive_failures} consecutive failures"
            )

    def request_restart(self, reason: str) -> None:
        """Mark the browser for relaunch before the next lookup."""
        if not self._restart_pending:
            logger.warning("Browser restart requested: %s", reason)
        self._restart_pending = True
        self._restart_reason = reason

    # ---- lifecycle --------------------------------------------------------

    def _needs_restart(self) -> tuple[bool, str]:
        if self._browser is None:
            return True, "no browser"
        if not self._browser.is_connected():
            return True, "browser disconnected"
        if self._restart_pending:
            return True, self._restart_reason or "restart requested"
        max_lookups = settings.browser_max_lookups
        if max_lookups > 0 and self._health.lookups_since_browser_launch >= max_lookups:
            return True, f"reached {max_lookups} lookups since launch"
        return False, ""

    async def _close_browser_locked(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.exception("Error closing browser")
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.exception("Error stopping playwright")
            self._playwright = None

    async def ensure_browser(self) -> bool:
        """Launch or relaunch the browser as needed. Returns True if restarted."""
        async with self._lock:
            needs, reason = self._needs_restart()
            if not needs:
                return False

            restarted = self._browser is not None
            if restarted:
                logger.warning("Recycling browser: %s", reason)
                await self._close_browser_locked()

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-background-networking",
                ],
            )
            self._health.browser_launched_at = _now_iso()
            self._health.lookups_since_browser_launch = 0
            self._restart_pending = False
            self._restart_reason = None
            if restarted:
                self._health.restarts += 1
            logger.info("Browser launched (restarted=%s)", restarted)
            return restarted

    @asynccontextmanager
    async def lookup_context(self) -> AsyncIterator[tuple[Page, bool]]:
        """Yield a page on a brand-new context, plus whether we just restarted.

        The context is always closed, so no DOM or cookie state survives into
        the next lookup.
        """
        restarted = await self.ensure_browser()
        browser = self._browser
        if browser is None:  # pragma: no cover - ensure_browser guarantees this
            raise RuntimeError("browser unavailable after ensure_browser()")

        context: Optional[BrowserContext] = None
        started = time.monotonic()
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 900},
                locale="en-CA",
            )
            await context.route("**/*", _route_handler)
            page = await context.new_page()
            yield page, restarted
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    logger.exception("Error closing browser context")
            logger.debug(
                "lookup context lifetime %.2fs", time.monotonic() - started
            )

    async def shutdown(self) -> None:
        async with self._lock:
            await self._close_browser_locked()


browser_manager = BrowserManager()
