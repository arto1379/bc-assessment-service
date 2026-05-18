"""Send alerts via Telegram Bot API (non-blocking)."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


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
                    json={"chat_id": chat_id, "text": text},
                )
                response.raise_for_status()
            except Exception:
                logger.exception("Telegram send failed for chat_id=%s", chat_id)


def send_telegram_message_background(text: str) -> None:
    """Fire-and-forget Telegram notification."""
    if not _enabled():
        return
    asyncio.create_task(send_telegram_message(text))
