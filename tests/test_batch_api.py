"""POST/GET/DELETE /batch and the CSV export, through the mock provider."""

from __future__ import annotations

import csv
import io
import json
import time

import pytest

from app.config import settings
from app.services import batch as batch_service

API_KEY = "test-batch-key"
HEADERS = {"X-API-Key": API_KEY}
RESULT_FIELDS = {
    "total_value",
    "year_built",
    "number_of_bed",
    "number_of_baths",
    "description",
    "land_size",
    "strata_area",
    "building_storeys",
}


@pytest.fixture
def batch_enabled(mock_provider, monkeypatch):
    monkeypatch.setattr(settings, "batch_enabled", True)
    monkeypatch.setattr(settings, "batch_api_keys", f"other-key, {API_KEY}")
    monkeypatch.setattr(batch_service, "_submit_lock", None)
    batch_service.batch_worker.reset()
    return settings


@pytest.fixture
def batch_client(batch_enabled):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def idle_client(batch_enabled, monkeypatch):
    """Batch enabled but the worker never runs, so pending items stay pending."""
    from fastapi.testclient import TestClient

    from app.main import app

    async def no_worker():
        return None

    monkeypatch.setattr(batch_service.batch_worker, "start", no_worker)
    with TestClient(app) as test_client:
        yield test_client


def submit(client, addresses, **extra):
    return client.post("/batch", json={"addresses": addresses, **extra}, headers=HEADERS)


def wait_for(client, job_id, states=("completed",), timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(f"/batch/{job_id}", headers=HEADERS).json()["data"]
        if body["state"] in states or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


class TestAccess:
    def test_disabled_by_default_returns_503(self, client):
        response = submit(client, ["1 A ST"])
        assert response.status_code == 503
        assert response.json()["status"] == "error"

    def test_missing_api_key_is_401(self, batch_client):
        response = batch_client.post("/batch", json={"addresses": ["1 A ST"]})
        assert response.status_code == 401
        assert response.json()["error_type"] == "unauthorized"

    def test_wrong_api_key_is_401(self, batch_client):
        response = batch_client.get("/batch/abc", headers={"X-API-Key": "nope"})
        assert response.status_code == 401

    def test_no_configured_keys_locks_everyone_out(self, batch_client, monkeypatch):
        monkeypatch.setattr(settings, "batch_api_keys", "")
        assert submit(batch_client, ["1 A ST"]).status_code == 401


class TestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {"addresses": []},
            {"addresses": ["1 A ST", "   "]},
            {"addresses": ["x" * 201]},
            {"addresses": ["1 A ST"], "callback_url": "http://hooks.example.com/x"},
            {"addresses": ["1 A ST"], "callback_url": "not a url"},
            {"addresses": "1 A ST"},
            {},
        ],
    )
    def test_invalid_bodies_are_400(self, batch_client, body):
        response = batch_client.post("/batch", json=body, headers=HEADERS)
        assert response.status_code == 400, response.text
        assert response.json()["error_type"] == "invalid_input"

    def test_oversize_list_is_400(self, batch_client, monkeypatch):
        monkeypatch.setattr(settings, "batch_max_addresses", 3)
        response = submit(batch_client, ["1 A ST", "2 A ST", "3 A ST", "4 A ST"])
        assert response.status_code == 400
        assert "limit is 3" in response.json()["message"]

    def test_bad_pagination_is_400(self, batch_client):
        assert batch_client.get("/batch/x?limit=0", headers=HEADERS).status_code == 400

    def test_address_endpoint_still_uses_422(self, batch_client):
        assert batch_client.get("/address").status_code == 422


class TestSubmitAndStatus:
    def test_submit_returns_202_with_the_documented_fields(self, batch_client):
        response = submit(
            batch_client, ["7009 TISDALL ST VANCOUVER", "12 EXAMPLE AVE BURNABY"],
            client_ref="import-1",
        )
        assert response.status_code == 202
        data = response.json()["data"]
        assert set(data) == {
            "job_id", "state", "total", "unique", "cached", "pending_scrape",
            "estimated_seconds",
        }
        assert data["total"] == 2 and data["unique"] == 2 and data["pending_scrape"] == 2
        assert data["estimated_seconds"] > 0

        status = wait_for(batch_client, data["job_id"])
        assert status["state"] == "completed"
        assert status["client_ref"] == "import-1"
        assert status["progress"] == {
            "done": 2, "not_found": 0, "failed": 0, "pending": 0, "cancelled": 0,
        }
        for item in status["items"]:
            assert set(item) == {"index", "address", "state", "source", "error_type", "result"}
            assert set(item["result"]) == RESULT_FIELDS
            assert item["source"] == "scrape"

    def test_items_match_what_address_returns(self, batch_client):
        job = submit(batch_client, ["1055 HOMER ST VANCOUVER"]).json()["data"]
        item = wait_for(batch_client, job["job_id"])["items"][0]
        live = batch_client.get("/address", params={"address": "1055 HOMER ST VANCOUVER"})
        assert item["result"] == live.json()["data"]["result"]

    def test_duplicates_are_resolved_once_and_repeated_at_every_index(self, batch_client):
        before = batch_client.get("/stats").json()["bc_calls"]
        job = submit(
            batch_client, ["5 DUP ST VANCOUVER", " 5 dup  st vancouver", "5 DUP ST VANCOUVER"]
        ).json()["data"]
        assert job["total"] == 3 and job["unique"] == 1

        status = wait_for(batch_client, job["job_id"])
        values = {item["result"]["total_value"] for item in status["items"]}
        assert [i["index"] for i in status["items"]] == [0, 1, 2]
        assert status["items"][1]["address"] == "5 dup  st vancouver"
        assert len(values) == 1
        assert batch_client.get("/stats").json()["bc_calls"] - before == 1

    def test_cached_addresses_resolve_at_submit_without_scraping(self, batch_client):
        batch_client.get("/address", params={"address": "7 CACHED ST"})
        batch_client.get("/address", params={"address": "7 NOWHERE ST"})
        before = batch_client.get("/stats").json()["bc_calls"]

        data = submit(batch_client, ["7 cached st", "7 NOWHERE ST"]).json()["data"]

        assert data["state"] == "completed"
        assert data["cached"] == 2 and data["pending_scrape"] == 0
        assert data["estimated_seconds"] == 0
        status = batch_client.get(f"/batch/{data['job_id']}", headers=HEADERS).json()["data"]
        states = [(i["state"], i["source"], i["error_type"]) for i in status["items"]]
        assert states == [("done", "cache", None), ("not_found", "cache", "not_found")]
        assert batch_client.get("/stats").json()["bc_calls"] == before

    def test_failed_items_carry_the_error_type(self, batch_client, monkeypatch):
        monkeypatch.setattr(settings, "batch_item_max_attempts", 1)
        data = submit(batch_client, ["9 BLOCKED ST", "9 NOWHERE ST"]).json()["data"]
        status = wait_for(batch_client, data["job_id"])
        assert [(i["state"], i["error_type"]) for i in status["items"]] == [
            ("failed", "blocked"),
            ("not_found", "not_found"),
        ]
        assert status["progress"]["failed"] == 1

    def test_distinct_addresses_never_share_a_total_value(self, batch_client):
        addresses = [f"{n} STALE CHECK ST VANCOUVER" for n in range(40)]
        data = submit(batch_client, addresses).json()["data"]
        status = wait_for(batch_client, data["job_id"])
        values = [i["result"]["total_value"] for i in status["items"]]
        assert len(set(values)) == len(addresses)

    def test_pagination(self, idle_client):
        data = submit(idle_client, [f"{n} PAGE ST" for n in range(7)]).json()["data"]
        job_id = data["job_id"]

        seen, offset = [], 0
        while offset is not None:
            page = idle_client.get(
                f"/batch/{job_id}", params={"offset": offset, "limit": 3}, headers=HEADERS
            ).json()["data"]
            seen.extend(i["index"] for i in page["items"])
            offset = page["next_offset"]

        assert seen == list(range(7))

    def test_unknown_job_is_404(self, batch_client):
        assert batch_client.get("/batch/nope", headers=HEADERS).status_code == 404
        assert batch_client.delete("/batch/nope", headers=HEADERS).status_code == 404


class TestCancel:
    def test_cancel_stops_pending_items_and_keeps_results(self, idle_client):
        idle_client.get("/address", params={"address": "1 KEPT ST"})
        data = submit(idle_client, ["1 KEPT ST", "2 PENDING ST", "3 PENDING ST"]).json()["data"]

        response = idle_client.delete(f"/batch/{data['job_id']}", headers=HEADERS)
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["state"] == "cancelled"
        assert body["progress"] == {
            "done": 1, "not_found": 0, "failed": 0, "pending": 0, "cancelled": 2,
        }

        status = idle_client.get(f"/batch/{data['job_id']}", headers=HEADERS).json()["data"]
        assert status["items"][0]["result"]["total_value"]
        assert status["completed_at"]

    def test_cancel_is_idempotent(self, idle_client):
        data = submit(idle_client, ["2 PENDING ST"]).json()["data"]
        first = idle_client.delete(f"/batch/{data['job_id']}", headers=HEADERS)
        second = idle_client.delete(f"/batch/{data['job_id']}", headers=HEADERS)
        assert first.json() == second.json()


class TestLimits:
    def test_active_job_limit_is_429(self, idle_client, monkeypatch):
        monkeypatch.setattr(settings, "batch_max_active_jobs", 1)
        assert submit(idle_client, ["1 FIRST ST"]).status_code == 202
        response = submit(idle_client, ["1 SECOND ST"])
        assert response.status_code == 429
        assert response.json()["error_type"] == "limit_reached"

    def test_pending_item_limit_is_429(self, idle_client, monkeypatch):
        monkeypatch.setattr(settings, "batch_max_pending_items", 2)
        assert submit(idle_client, ["1 A ST", "2 A ST", "3 A ST"]).status_code == 429
        assert submit(idle_client, ["1 A ST", "2 A ST"]).status_code == 202

    def test_a_fully_cached_job_is_never_limited(self, idle_client, monkeypatch):
        monkeypatch.setattr(settings, "batch_max_active_jobs", 1)
        idle_client.get("/address", params={"address": "1 CACHED ST"})
        assert submit(idle_client, ["1 PENDING ST"]).status_code == 202
        assert submit(idle_client, ["1 CACHED ST"]).status_code == 202


class TestCsvExport:
    def test_csv_has_one_row_per_index(self, batch_client, monkeypatch):
        monkeypatch.setattr(settings, "batch_item_max_attempts", 1)
        data = submit(batch_client, ["7009 TISDALL ST VANCOUVER", "1 NOWHERE ST"]).json()["data"]
        wait_for(batch_client, data["job_id"])

        response = batch_client.get(f"/batch/{data['job_id']}/results.csv", headers=HEADERS)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert [r["index"] for r in rows] == ["0", "1"]
        assert rows[0]["state"] == "done" and rows[0]["total_value"].startswith("$")
        assert rows[1]["state"] == "not_found" and rows[1]["total_value"] == ""

    def test_csv_neutralises_formulas(self, idle_client):
        data = submit(idle_client, ["=HYPERLINK(1)"]).json()["data"]
        response = idle_client.get(f"/batch/{data['job_id']}/results.csv", headers=HEADERS)
        assert "'=HYPERLINK(1)" in response.text

    def test_csv_for_unknown_job_is_404(self, batch_client):
        assert batch_client.get("/batch/nope/results.csv", headers=HEADERS).status_code == 404


def test_address_success_response_is_byte_for_byte_unchanged(batch_client):
    """Acceptance: the /address success body is exactly what it was before."""
    from app.services.lookup import _mock_lookup

    address = "7009 TISDALL ST VANCOUVER"
    expected = _mock_lookup(address).result
    legacy = json.dumps(
        {"status": "success", "data": {"result": expected.model_dump(), "status": 200}},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()

    first = batch_client.get("/address", params={"address": address})
    cached = batch_client.get("/address", params={"address": address})

    assert first.content == legacy
    assert cached.content == legacy
    assert "Retry-After" not in first.headers
