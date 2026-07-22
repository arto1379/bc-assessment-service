"""BC Assessment scraping steps, block detection and HTML parsing.

Every step is named so that a failure can report *where* it broke, and each
failure is classified (`FailureType`) rather than collapsed into `None`. That
classification is what lets the API distinguish "this address does not exist"
from "we are being blocked".
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.config import settings
from app.schemas import AssessmentResult, FailureType, LookupOutcome

logger = logging.getLogger(__name__)

BC_URL = "https://www.bcassessment.ca/"
SEARCH_SELECTOR = "#rsbSearch"
AUTOCOMPLETE_LIST = ".ui-autocomplete"
AUTOCOMPLETE_ITEM = ".ui-autocomplete li"
RESULT_READY_JS = (
    "() => document.body && /Total value\\s*\\$?[\\d,]+/i.test(document.body.innerText)"
)

# Text that indicates BC Assessment (or a WAF in front of it) is refusing us
# rather than serving the site.
_BLOCK_MARKERS = (
    "access denied",
    "unusual traffic",
    "are you a robot",
    "captcha",
    "request rejected",
    "request blocked",
    "bot detected",
    "checking your browser",
    "attention required",
    "cf-browser-verification",
    "cf-challenge",
    "incapsula",
    "too many requests",
)

_NO_MATCH_RE = re.compile(r"no\s+(match|result|record)|not\s+found", re.I)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


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
        texts = [t for t in (_clean(s) for s in row.stripped_strings) if t]
        if len(texts) >= 2:
            return texts[-1]

    return None


def _find_total_value(soup: BeautifulSoup) -> Optional[str]:
    match = re.search(r"Total value\s*\$?([\d,]+)", soup.get_text(" "), re.I)
    return "$" + match.group(1) if match else None


def parse_html(html: str) -> AssessmentResult:
    """Parse a BC Assessment property page into the API result shape.

    `lxml` is used when available (noticeably faster and more forgiving on the
    malformed markup BC serves), falling back to the stdlib parser.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
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


# ---------------------------------------------------------------------------
# Block detection
# ---------------------------------------------------------------------------


def detect_block(html: str, url: str, status: Optional[int]) -> Optional[str]:
    """Return a human-readable reason if the page looks like a block, else None."""
    if status is not None and status >= 400:
        return f"HTTP {status} from BC Assessment"

    if url and "bcassessment.ca" not in url:
        return f"redirected off-site to {url}"

    lowered = html[:200_000].lower()
    for marker in _BLOCK_MARKERS:
        if marker in lowered:
            return f"block marker in page content: {marker!r}"

    return None


def save_snapshot(address: str, html: str) -> Optional[str]:
    """Persist page HTML for post-mortem debugging. Returns the path, if saved."""
    if not settings.save_html_snapshots or not html:
        return None
    try:
        root = Path(settings.snapshot_dir)
        if not root.is_absolute():
            root = Path(__file__).resolve().parent.parent.parent / root
        root.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        digest = hashlib.sha1(address.encode("utf-8")).hexdigest()[:10]
        path = root / f"{stamp}_{digest}.html"
        path.write_text(html, encoding="utf-8", errors="replace")

        _prune_snapshots(root)
        return str(path)
    except Exception:
        logger.exception("Failed to save HTML snapshot")
        return None


def _prune_snapshots(root: Path) -> None:
    keep = settings.snapshot_keep
    if keep <= 0:
        return
    files = sorted(root.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


async def _safe_content(page: Page) -> str:
    try:
        return await page.content()
    except Exception:
        return ""


async def _safe_url(page: Page) -> str:
    try:
        return page.url
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# The scrape
# ---------------------------------------------------------------------------


async def scrape_address(page: Page, address: str, *, restarted: bool) -> LookupOutcome:
    """Run one lookup on a freshly created page.

    The page is guaranteed clean by `BrowserManager.lookup_context()`, which is
    what makes the result-readiness check below trustworthy: on a fresh context
    there is no previous property's "Total value" left in the DOM to match.
    """
    step = "goto_home"
    try:
        # --- 1. load the home page -------------------------------------
        response = await page.goto(
            BC_URL, wait_until="domcontentloaded", timeout=30_000
        )
        status = response.status if response is not None else None
        html = await _safe_content(page)
        url = await _safe_url(page)

        blocked = detect_block(html, url, status)
        if blocked:
            return LookupOutcome.failed(
                FailureType.BLOCKED,
                blocked,
                url=url,
                step=step,
                restarted=restarted,
                snapshot_path=save_snapshot(address, html),
            )

        step = "await_search_box"
        try:
            await page.wait_for_selector(
                SEARCH_SELECTOR, state="visible", timeout=15_000
            )
        except PlaywrightTimeoutError:
            html = await _safe_content(page)
            url = await _safe_url(page)
            reason = detect_block(html, url, None) or "search box never appeared"
            return LookupOutcome.failed(
                FailureType.BLOCKED if "block" in reason or "redirect" in reason
                else FailureType.TIMEOUT,
                reason,
                url=url,
                step=step,
                restarted=restarted,
                snapshot_path=save_snapshot(address, html),
            )

        # --- 2. type the address ---------------------------------------
        step = "fill_search"
        search = page.locator(SEARCH_SELECTOR)
        await search.click(timeout=10_000)
        # `fill` (not `type`) is what the original scraper used and what BC's
        # autocomplete demonstrably responds to; keeping it avoids changing
        # proven behaviour on a site we cannot exercise from CI.
        await search.fill(address, timeout=10_000)

        # --- 3. wait for suggestions -----------------------------------
        step = "await_autocomplete"
        try:
            await page.wait_for_selector(
                AUTOCOMPLETE_ITEM,
                state="visible",
                timeout=settings.scrape_autocomplete_timeout_ms,
            )
        except PlaywrightTimeoutError:
            return await _classify_missing_autocomplete(
                page, address, step=step, restarted=restarted
            )

        items = page.locator(AUTOCOMPLETE_ITEM)
        count = await items.count()
        if count == 0:
            return await _classify_missing_autocomplete(
                page, address, step=step, restarted=restarted
            )

        first_text = _clean(await items.first.inner_text()) or ""
        if _NO_MATCH_RE.search(first_text):
            return LookupOutcome.failed(
                FailureType.NOT_FOUND,
                f"autocomplete reported no match ({first_text!r})",
                url=await _safe_url(page),
                step=step,
                restarted=restarted,
            )

        # --- 4. open the property ---------------------------------------
        step = "click_suggestion"
        home_url = await _safe_url(page)
        await items.first.click(timeout=10_000)

        step = "await_result_nav"
        try:
            await page.wait_for_url(
                lambda current: current != home_url,
                timeout=settings.scrape_result_timeout_ms,
            )
        except PlaywrightTimeoutError:
            # Some result flows update in place rather than navigating; the
            # content check below is the real gate, so this is not fatal yet.
            logger.debug("No URL change after suggestion click for %r", address)

        try:
            await page.wait_for_function(
                RESULT_READY_JS, timeout=settings.scrape_result_timeout_ms
            )
        except PlaywrightTimeoutError:
            html = await _safe_content(page)
            url = await _safe_url(page)
            reason = detect_block(html, url, None)
            return LookupOutcome.failed(
                FailureType.BLOCKED if reason else FailureType.TIMEOUT,
                reason or "property page never rendered a total value",
                url=url,
                step=step,
                restarted=restarted,
                snapshot_path=save_snapshot(address, html),
            )

        # --- 5. extract and parse ---------------------------------------
        step = "extract_html"
        html = await page.content()
        url = await _safe_url(page)

        blocked = detect_block(html, url, None)
        if blocked:
            return LookupOutcome.failed(
                FailureType.BLOCKED,
                blocked,
                url=url,
                step=step,
                restarted=restarted,
                snapshot_path=save_snapshot(address, html),
            )

        step = "parse"
        result = parse_html(html)
        if not result.total_value:
            return LookupOutcome.failed(
                FailureType.PARSE_EMPTY,
                "result page parsed but contained no total_value",
                url=url,
                step=step,
                restarted=restarted,
                snapshot_path=save_snapshot(address, html),
            )

        logger.info(
            "scrape ok address=%r url=%s total_value=%s",
            address,
            url,
            result.total_value,
        )
        return LookupOutcome.success(result)

    except PlaywrightTimeoutError as exc:
        return LookupOutcome.failed(
            FailureType.TIMEOUT,
            f"timeout during {step}: {exc}",
            url=await _safe_url(page),
            step=step,
            restarted=restarted,
        )
    except Exception as exc:
        logger.exception("scrape error address=%r step=%s", address, step)
        return LookupOutcome.failed(
            FailureType.ERROR,
            f"{type(exc).__name__} during {step}: {exc}",
            url=await _safe_url(page),
            step=step,
            restarted=restarted,
        )


async def _classify_missing_autocomplete(
    page: Page, address: str, *, step: str, restarted: bool
) -> LookupOutcome:
    """Decide why no suggestion appeared.

    This is the distinction the old code could not make. If the page is still
    healthy — search box present, our text still in it, no block markers — then
    BC simply has no property matching this address, which is a genuine
    NOT_FOUND. Otherwise the scraper itself is in trouble.
    """
    html = await _safe_content(page)
    url = await _safe_url(page)

    reason = detect_block(html, url, None)
    if reason:
        return LookupOutcome.failed(
            FailureType.BLOCKED,
            reason,
            url=url,
            step=step,
            restarted=restarted,
            snapshot_path=save_snapshot(address, html),
        )

    try:
        search_alive = await page.locator(SEARCH_SELECTOR).count() > 0
        typed = await page.locator(SEARCH_SELECTOR).first.input_value(timeout=5_000)
    except Exception:
        search_alive, typed = False, ""

    if search_alive and typed.strip():
        return LookupOutcome.failed(
            FailureType.NOT_FOUND,
            "no autocomplete suggestion for this address",
            url=url,
            step=step,
            restarted=restarted,
        )

    return LookupOutcome.failed(
        FailureType.TIMEOUT,
        "autocomplete never appeared and the search box was not usable",
        url=url,
        step=step,
        restarted=restarted,
        snapshot_path=save_snapshot(address, html),
    )
