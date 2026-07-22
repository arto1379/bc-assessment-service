"""End-to-end API tests through the mock provider (no browser, no network)."""

from __future__ import annotations

from app.db import failures


class TestSuccess:
    def test_response_matches_the_documented_shape(self, client):
        response = client.get("/address", params={"address": "7009 TISDALL ST VANCOUVER"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["data"]["status"] == 200
        assert set(body["data"]["result"]) == {
            "total_value",
            "year_built",
            "number_of_bed",
            "number_of_baths",
            "description",
            "land_size",
            "strata_area",
            "building_storeys",
        }
        assert body["data"]["result"]["total_value"].startswith("$")

    def test_repeat_request_is_served_from_cache(self, client):
        params = {"address": "1055 HOMER ST VANCOUVER"}
        first = client.get("/address", params=params).json()
        second = client.get("/address", params=params).json()

        assert first == second

    def test_address_is_normalized_for_the_cache_key(self, client):
        a = client.get("/address", params={"address": "  1055 homer  st vancouver "})
        b = client.get("/address", params={"address": "1055 HOMER ST VANCOUVER"})

        assert a.json()["data"]["result"] == b.json()["data"]["result"]

    def test_distinct_addresses_get_distinct_values(self, client):
        """Regression guard for the stale-page defect."""
        seen = set()
        for address in ("A ST VANCOUVER", "B ST VANCOUVER", "C ST VANCOUVER"):
            value = client.get("/address", params={"address": address}).json()
            seen.add(value["data"]["result"]["total_value"])

        assert len(seen) == 3


class TestFailures:
    def test_unknown_address_is_reported_as_not_found(self, client):
        response = client.get("/address", params={"address": "99999 NOWHERE ST"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "error"
        # Message preserved exactly for backward compatibility.
        assert body["message"] == "Property not found"
        assert body["error_type"] == "not_found"

    def test_blocked_is_distinguishable_from_not_found(self, client):
        """The core fix: a blocked scraper must not look like a missing property."""
        response = client.get("/address", params={"address": "BLOCKED ST VANCOUVER"})

        body = response.json()
        assert body["status"] == "error"
        assert body["error_type"] == "blocked"
        assert body["message"] != "Property not found"

    def test_whitespace_address_returns_a_clear_error(self, client):
        response = client.get("/address", params={"address": "   "})

        body = response.json()
        assert body["status"] == "error"
        assert body["error_type"] == "invalid_input"

    def test_missing_address_is_rejected(self, client):
        assert client.get("/address").status_code == 422

    def test_empty_address_is_rejected(self, client):
        assert client.get("/address", params={"address": ""}).status_code == 422

    def test_malformed_input_never_500s(self, client):
        for address in ("!!!", "a" * 500, "'; DROP TABLE assessment_cache; --", "<script>"):
            response = client.get("/address", params={"address": address})
            assert response.status_code < 500, address

    def test_failed_lookup_is_queued_for_retry(self, client):
        client.get("/address", params={"address": "BLOCKED ST VANCOUVER"})

        queued = failures.list_failures()
        assert len(queued) == 1
        assert queued[0]["failure_type"] == "blocked"
        assert queued[0]["reason"]

    def test_not_found_is_not_rescraped(self, client):
        """Second call is served by the negative cache, not another scrape."""
        params = {"address": "1 DOESNOTEXIST BLVD"}
        client.get("/address", params=params)
        before = client.get("/stats").json()["bc_calls"]
        client.get("/address", params=params)
        after = client.get("/stats").json()["bc_calls"]

        # Guards the guard: if the counter never moved, this test would pass
        # for the wrong reason.
        assert before >= 1
        assert after == before


class TestOperationalEndpoints:
    def test_health_reports_scraper_state(self, client):
        body = client.get("/health").json()

        assert body["ok"] is True
        for key in (
            "browser_connected",
            "consecutive_failures",
            "last_success_at",
            "lookups_since_browser_launch",
            "unresolved_failures",
        ):
            assert key in body

    def test_stats_keeps_its_original_keys(self, client):
        body = client.get("/stats").json()

        assert "cache_calls" in body
        assert "bc_calls" in body

    def test_failures_endpoint_lists_the_queue(self, client):
        client.get("/address", params={"address": "BLOCKED ST VANCOUVER"})
        body = client.get("/failures").json()

        assert body["counts"]["blocked"] == 1
        assert body["items"][0]["failure_type"] == "blocked"
