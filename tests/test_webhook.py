"""Batch webhooks: signature, allowlist, refused redirects, retries, SSRF guard."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest

from app.config import settings
from app.db import batch as store
from app.services import webhook

SECRET = "webhook-test-secret"
HOOK_URL = "https://hooks.example.com/bc"


@pytest.fixture
def hooks_on(monkeypatch):
    monkeypatch.setattr(settings, "batch_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "batch_callback_allowed_hosts", "hooks.example.com")
    monkeypatch.setattr(webhook, "BACKOFF_BASE_SECONDS", 0.0)

    async def public(host, port):
        return True

    monkeypatch.setattr(webhook, "resolves_to_public", public)


@pytest.fixture
def transport(monkeypatch):
    """Scripted responses; records every request the webhook sends."""

    class Script:
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self.statuses: list[int] = [200]

        def handler(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            headers = {"Location": "https://evil.internal/"} if 300 <= status < 400 else {}
            return httpx.Response(status, headers=headers)

    script = Script()
    monkeypatch.setattr(webhook, "_transport", httpx.MockTransport(script.handler))
    return script


def _job(job_id="job1", callback_url=HOOK_URL, state=store.COMPLETED):
    store.create_job(
        job_id,
        state=state,
        client_ref=None,
        callback_url=callback_url,
        items=[
            {"idx": 0, "address_raw": "1 A ST", "address_key": "1 A ST", "state": store.DONE},
            {"idx": 1, "address_raw": "2 B ST", "address_key": "2 B ST", "state": store.NOT_FOUND},
        ],
    )
    return job_id


class TestValidation:
    def test_disabled_without_allowlist_or_secret(self, monkeypatch):
        monkeypatch.setattr(settings, "batch_callback_allowed_hosts", "")
        monkeypatch.setattr(settings, "batch_webhook_secret", SECRET)
        assert "disabled" in webhook.validate_callback_url(HOOK_URL)

        monkeypatch.setattr(settings, "batch_callback_allowed_hosts", "hooks.example.com")
        monkeypatch.setattr(settings, "batch_webhook_secret", "")
        assert "disabled" in webhook.validate_callback_url(HOOK_URL)

    @pytest.mark.parametrize(
        "url, reason",
        [
            ("http://hooks.example.com/bc", "https"),
            ("https://evil.example.com/bc", "allowlist"),
            ("https://hooks.example.com.evil.net/bc", "allowlist"),
            ("https://user:pw@hooks.example.com/bc", "credentials"),
            ("ftp://hooks.example.com/bc", "https"),
        ],
    )
    def test_rejections(self, hooks_on, url, reason):
        assert reason in webhook.validate_callback_url(url)

    def test_allowed_host_is_case_insensitive(self, hooks_on):
        assert webhook.validate_callback_url("https://HOOKS.example.com/x") is None

    async def test_private_and_loopback_addresses_are_refused(self):
        assert await webhook.resolves_to_public("127.0.0.1", 443) is False
        assert await webhook.resolves_to_public("10.0.0.5", 443) is False
        assert await webhook.resolves_to_public("169.254.169.254", 443) is False
        assert await webhook.resolves_to_public("::1", 443) is False
        assert await webhook.resolves_to_public("8.8.8.8", 443) is True


class TestDelivery:
    async def test_body_is_signed_with_hmac_sha256(self, hooks_on, transport):
        job_id = _job()
        assert await webhook.deliver(job_id) is True

        request = transport.requests[0]
        raw = request.content
        expected = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        assert request.headers["X-BC-Signature"] == f"sha256={expected}"
        assert json.loads(raw) == {
            "job_id": job_id,
            "state": "completed",
            "total": 2,
            "progress": {"done": 1, "not_found": 1, "failed": 0, "pending": 0, "cancelled": 0},
        }

    async def test_non_2xx_is_retried_three_times(self, hooks_on, transport):
        transport.statuses = [500, 502, 503, 200]
        assert await webhook.deliver(_job()) is True
        assert len(transport.requests) == 4

    async def test_gives_up_after_three_retries(self, hooks_on, transport):
        transport.statuses = [500]
        assert await webhook.deliver(_job()) is False
        assert len(transport.requests) == 4

    async def test_redirects_are_refused_not_followed(self, hooks_on, transport):
        transport.statuses = [302]
        assert await webhook.deliver(_job()) is False
        assert {str(r.url) for r in transport.requests} == {HOOK_URL}

    async def test_a_non_public_host_is_never_contacted(self, hooks_on, transport, monkeypatch):
        async def private(host, port):
            return False

        monkeypatch.setattr(webhook, "resolves_to_public", private)
        assert await webhook.deliver(_job()) is False
        assert transport.requests == []

    async def test_host_removed_from_allowlist_after_submit_is_not_called(
        self, hooks_on, transport, monkeypatch
    ):
        job_id = _job()
        monkeypatch.setattr(settings, "batch_callback_allowed_hosts", "other.example.com")
        assert await webhook.deliver(job_id) is False
        assert transport.requests == []

    async def test_job_without_callback_sends_nothing(self, hooks_on, transport):
        assert await webhook.deliver(_job(callback_url=None)) is False
        assert transport.requests == []


class TestEndToEnd:
    def test_completed_job_posts_its_webhook(self, mock_provider, hooks_on, transport, monkeypatch):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.services import batch as batch_service

        monkeypatch.setattr(settings, "batch_enabled", True)
        monkeypatch.setattr(settings, "batch_api_keys", "k")
        monkeypatch.setattr(batch_service, "_submit_lock", None)
        batch_service.batch_worker.reset()

        with TestClient(app) as client:
            rejected = client.post(
                "/batch",
                json={"addresses": ["1 A ST"], "callback_url": "https://evil.example.com/x"},
                headers={"X-API-Key": "k"},
            )
            assert rejected.status_code == 400

            job = client.post(
                "/batch",
                json={"addresses": ["1 HOOK ST", "2 HOOK ST"], "callback_url": HOOK_URL},
                headers={"X-API-Key": "k"},
            ).json()["data"]

            deadline = time.monotonic() + 10
            while not transport.requests and time.monotonic() < deadline:
                time.sleep(0.05)

        assert len(transport.requests) == 1
        payload = json.loads(transport.requests[0].content)
        assert payload["job_id"] == job["job_id"]
        assert payload["state"] == "completed"
        assert "items" not in payload, "the webhook carries no property data"
