"""Send alerts via Telegram Bot API (non-blocking)."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# asyncio only holds a weak reference to a running task, so a fire-and-forget
# task can be garbage-collected mid-flight and the alert silently lost. Keeping
# strong references until completion is the documented fix.
_background_tasks: set[asyncio.Task] = set()


def _enabled() -> bool:
    return bool(settings.telegram_bot_token and settings.get_telegram_chat_ids())


async def send_telegram_message(text: str) -> None:
    if not _enabled():
        logger.debug("Telegram disabled (missing token or chat ids)")
        return

    url = _TELEGRAM_API.format(token=settings.telegram_bot_token)
    async with httpx.AsyncClient(timeout=15.0) as client:
        for chat_id in settings.get_telegram_chat_ids():
            try:
                response = await client.post(
                    url,
                    json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                )
                response.raise_for_status()
            except Exception:
                logger.exception("Telegram send failed for chat_id=%s", chat_id)


def send_telegram_message_background(text: str) -> None:
    """Fire-and-forget Telegram notification.

    Falls back to logging when there is no running event loop (e.g. called from
    a synchronous script or a test), so alerting can never raise into a lookup.
    """
    if not _enabled():
        logger.debug("Telegram alert suppressed (disabled): %s", text.splitlines()[0])
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("No event loop for Telegram alert; not sent: %s", text)
        return

    task = loop.create_task(send_telegram_message(text))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def flush_pending(timeout: float = 5.0) -> None:
    """Wait briefly for in-flight alerts, used on shutdown."""
    if not _background_tasks:
        return
    await asyncio.wait(set(_background_tasks), timeout=timeout)
