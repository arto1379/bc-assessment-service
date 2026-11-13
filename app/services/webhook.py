"""Batch completion webhooks: HMAC-signed, https only, allowlisted hosts.

On `completed` or `cancelled` the service POSTs `{job_id, state, total,
progress}` to the job's callback_url with

    X-BC-Signature: sha256=<hex HMAC-SHA256 of the raw body, key BATCH_WEBHOOK_SECRET>

The webhook carries no property data; the receiver fetches it with
GET /batch/{job_id}. Server-side request forgery guards: https only, the host
must be in BATCH_CALLBACK_ALLOWED_HOSTS, redirects are never followed (a 3xx
counts as a failed attempt), a 5 s timeout, and hosts that resolve to private,
loopback, link-local or otherwise non-public addresses are refused. A non-2xx
reply is retried 3 times with exponential backoff.

Webhooks are enabled only when both the allowlist and the secret are set; with
either empty, a submit that asks for a callback is rejected so the caller knows
to poll instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from typing import Optional
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.db import batch as store
from app.services.telegram_notify import run_in_background

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5.0
RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0  # 2 s, 4 s, 8 s between attempts
SIGNATURE_HEADER = "X-BC-Signature"

# Test seam: an httpx transport (e.g. MockTransport) used instead of the network.
_transport: Optional[httpx.AsyncBaseTransport] = None


def webhooks_enabled() -> bool:
    return bool(settings.get_callback_allowed_hosts() and settings.batch_webhook_secret)


def validate_callback_url(url: str) -> Optional[str]:
    """Return why a callback_url is unacceptable, or None if it is fine."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "callback_url must be an https URL"
    if parts.scheme != "https" or not host:
        return "callback_url must be an https URL"
    if parts.username or parts.password:
        return "callback_url must not contain credentials"
    if not webhooks_enabled():
        return "webhooks are disabled on this service; poll GET /batch/{job_id} instead"
    if host.lower() not in settings.get_callback_allowed_hosts():
        return f"callback_url host {host!r} is not in the allowlist"
    return None


def sign(body: bytes) -> str:
    digest = hmac.new(settings.batch_webhook_secret.encode(), body, hashlib.sha256)
    return f"sha256={digest.hexdigest()}"


async def resolves_to_public(host: str, port: int) -> bool:
    """True only if every address the host resolves to is publicly routable."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            if not ipaddress.ip_address(address).is_global:
                return False
        except ValueError:
            return False
    return True


def _payload(job_id: str) -> Optional[tuple[str, bytes]]:
    job = store.get_job(job_id)
    if job is None or not job["callback_url"]:
        return None
    body = json.dumps(
        {
            "job_id": job["id"],
            "state": job["state"],
            "total": job["total"],
            "progress": store.job_progress(job_id),
        },
        separators=(",", ":"),
    ).encode()
    return job["callback_url"], body


async def deliver(job_id: str) -> bool:
    """POST the completion webhook for a job. Returns True on a 2xx reply."""
    prepared = await asyncio.to_thread(_payload, job_id)
    if prepared is None:
        return False
    url, body = prepared

    # Re-check at send time: the allowlist or secret may have changed since submit.
    problem = validate_callback_url(url)
    if problem:
        logger.warning("webhook not sent job_id=%s: %s", job_id, problem)
        return False
    parts = urlsplit(url)
    if not await resolves_to_public(parts.hostname or "", parts.port or 443):
        logger.warning(
            "webhook refused job_id=%s: %s resolves to a non-public address",
            job_id,
            parts.hostname,
        )
        return False

    headers = {"Content-Type": "application/json", SIGNATURE_HEADER: sign(body)}
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=_transport
    ) as client:
        for attempt in range(RETRIES + 1):
            try:
                response = await client.post(url, content=body, headers=headers)
                if 200 <= response.status_code < 300:
                    logger.info(
                        "webhook delivered job_id=%s status=%s attempt=%d",
                        job_id,
                        response.status_code,
                        attempt + 1,
                    )
                    return True
                logger.warning(
                    "webhook job_id=%s got HTTP %s (attempt %d)%s",
                    job_id,
                    response.status_code,
                    attempt + 1,
                    "; redirects are not followed" if response.is_redirect else "",
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "webhook job_id=%s failed (attempt %d): %s",
                    job_id,
                    attempt + 1,
                    type(exc).__name__,
                )
            if attempt < RETRIES:
                await asyncio.sleep(BACKOFF_BASE_SECONDS * 2**attempt)

    logger.error("webhook gave up job_id=%s after %d attempts", job_id, RETRIES + 1)
    return False


def deliver_background(job_id: str) -> None:
    run_in_background(deliver(job_id))
