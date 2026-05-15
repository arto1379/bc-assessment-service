import asyncio
import logging
import re
import time
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from app.config import settings
from app.schemas import AssessmentResult

logger = logging.getLogger(__name__)

BC_URL = "https://www.bcassessment.ca/"
SEARCH_SELECTOR = "#rsbSearch"
AUTOCOMPLETE_SELECTOR = ".ui-autocomplete li"
RESULT_READY_JS = (
    "() => document.body && /Total value\\s*\\$?[\\d,]+/i.test(document.body.innerText)"
)

_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
_BLOCKED_URL_RE = re.compile(
    r"(google-analytics|googletagmanager|doubleclick|facebook|hotjar|clarity)",
    re.I,
)

_playwright: Optional[Playwright] = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_browser_lock = asyncio.Lock()
_scrape_lock = asyncio.Semaphore(1)

_cache: dict[str, tuple[float, AssessmentResult]] = {}


def _normalize_address(address: str) -> str:
    return re.sub(r"\s+", " ", address.strip().upper())


def _cache_get(key: str) -> Optional[AssessmentResult]:
    if settings.scrape_cache_ttl_seconds <= 0:
        return None
    entry = _cache.get(key)
    if not entry:
        return None
    expires_at, result = entry
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        return None
    return result


def _cache_set(key: str, result: AssessmentResult) -> None:
    if settings.scrape_cache_ttl_seconds <= 0:
        return
    _cache[key] = (
        time.monotonic() + settings.scrape_cache_ttl_seconds,
        result,
    )


def _clean(text: str | None) -> Optional[str]:
    return re.sub(r"\s+", " ", text).strip() if text else None


def _find_value_after_label(soup: BeautifulSoup, label: str) -> Optional[str]:
    label_node = soup.find(string=re.compile(rf"^{re.escape(label)}$", re.I))
    if not label_node:
        return None

    parent = label_node.find_parent()
    if not parent:
        return None

    for sib in parent.find_next_siblings():
        txt = _clean(sib.get_text(" "))
        if txt:
            return txt

    row = parent.find_parent()
    if row:
        texts = [_clean(t) for t in row.stripped_strings]
        texts = [t for t in texts if t]
        if len(texts) >= 2:
            return texts[-1]

    return None


def _find_total_value(soup: BeautifulSoup) -> Optional[str]:
    text = soup.get_text(" ")
    match = re.search(r"Total value\s*\$?([\d,]+)", text, re.I)
    if match:
        return "$" + match.group(1)
    return None


def _parse_html(html: str) -> AssessmentResult:
    soup = BeautifulSoup(html, "html.parser")
    return AssessmentResult(
        total_value=_find_total_value(soup),
        year_built=_find_value_after_label(soup, "Year built"),
        number_of_bed=_find_value_after_label(soup, "Bedrooms"),
        number_of_baths=_find_value_after_label(soup, "Baths"),
        description=_find_value_after_label(soup, "Description"),
        land_size=_find_value_after_label(soup, "Land size"),
        strata_area=_find_value_after_label(soup, "Strata area"),
        building_storeys=_find_value_after_label(soup, "Building storeys"),
    )


async def _route_handler(route: Route) -> None:
    request = route.request
    if request.resource_type in _BLOCKED_RESOURCE_TYPES:
        await route.abort()
        return
    if _BLOCKED_URL_RE.search(request.url):
        await route.abort()
        return
    await route.continue_()


async def _get_browser() -> Browser:
    global _playwright, _browser
    async with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser

        if _browser is not None:
            await _browser.close()
            _browser = None
        if _playwright is not None:
            await _playwright.stop()
            _playwright = None

        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    return _browser


async def _get_context() -> BrowserContext:
    global _context
    if _context is not None:
        return _context

    browser = await _get_browser()
    _context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    await _context.route("**/*", _route_handler)
    return _context


async def _get_page() -> Page:
    global _page
    if _page is not None and not _page.is_closed():
        return _page

    context = await _get_context()
    _page = await context.new_page()
    return _page


async def _ensure_home(page: Page) -> None:
    if await page.locator(SEARCH_SELECTOR).count() > 0:
        return
    await page.goto(BC_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_selector(SEARCH_SELECTOR, state="visible", timeout=15_000)


async def warmup_browser() -> None:
    """Pre-load BC Assessment so the first API request is faster."""
    if not settings.scrape_warmup_on_start:
        return
    async with _scrape_lock:
        page = await _get_page()
        await _ensure_home(page)
    logger.info("BC Assessment browser warmed up")


async def _scrape_bcassessment(address: str) -> Optional[AssessmentResult]:
    page = await _get_page()

    try:
        await _ensure_home(page)

        search = page.locator(SEARCH_SELECTOR)
        await search.click()
        await search.fill("")
        await search.fill(address)

        await page.wait_for_selector(
            AUTOCOMPLETE_SELECTOR,
            state="visible",
            timeout=settings.scrape_autocomplete_timeout_ms,
        )
        await page.locator(f"{AUTOCOMPLETE_SELECTOR}").first.click()

        await page.wait_for_function(
            RESULT_READY_JS,
            timeout=settings.scrape_result_timeout_ms,
        )

        html = await page.content()
        result = _parse_html(html)

        if not result.total_value:
            logger.warning(
                "BC scrape: no total_value for address=%r url=%s", address, page.url
            )
            return None

        return result

    except Exception as e:
        logger.exception("BC scrape failed for address=%r: %s", address, e)
        global _page
        if _page is not None and not _page.is_closed():
            await _page.close()
        _page = None
        return None


async def lookup_by_address(address: str) -> Optional[AssessmentResult]:
    """Return assessment data for an address, or None if not found."""
    if not address or not address.strip():
        return None

    normalized = _normalize_address(address)
    cached = _cache_get(normalized)
    if cached is not None:
        logger.debug("BC scrape cache hit for address=%r", address)
        return cached

    async with _scrape_lock:
        cached = _cache_get(normalized)
        if cached is not None:
            return cached

        result = await _scrape_bcassessment(address.strip())
        if result is not None:
            _cache_set(normalized, result)
        return result


async def shutdown_browser() -> None:
    global _playwright, _browser, _context, _page, _cache
    async with _browser_lock:
        if _page is not None and not _page.is_closed():
            await _page.close()
        _page = None
        if _context is not None:
            await _context.close()
        _context = None
        if _browser is not None:
            await _browser.close()
        _browser = None
        if _playwright is not None:
            await _playwright.stop()
        _playwright = None
    _cache.clear()
