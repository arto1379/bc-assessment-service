"""Parsing and block-detection tests — no browser, no network."""

from __future__ import annotations

import pytest

from app.services.scraper import detect_block, parse_html
from tests.conftest import BLOCKED_HTML, CAPTCHA_HTML, NO_VALUE_HTML, PROPERTY_HTML


def test_parses_a_full_property_page():
    result = parse_html(PROPERTY_HTML)

    assert result.total_value == "$817,000"
    assert result.year_built == "1989"
    assert result.number_of_bed == "2"
    assert result.number_of_baths == "2"
    assert result.strata_area == "1100"
    assert result.building_storeys == "12"


def test_page_without_a_total_value_parses_to_none():
    # This is what drives FailureType.PARSE_EMPTY rather than a bogus success.
    assert parse_html(NO_VALUE_HTML).total_value is None


def test_parsing_malformed_html_does_not_raise():
    assert parse_html("<html><body><div>unclosed").total_value is None
    assert parse_html("").total_value is None


class TestBlockDetection:
    def test_clean_page_is_not_flagged(self):
        assert detect_block(PROPERTY_HTML, "https://www.bcassessment.ca/x", 200) is None

    def test_http_error_status_is_a_block(self):
        reason = detect_block(PROPERTY_HTML, "https://www.bcassessment.ca/", 403)
        assert reason is not None and "403" in reason

    @pytest.mark.parametrize("status", [429, 503])
    def test_throttling_statuses_are_blocks(self, status):
        assert detect_block("", "https://www.bcassessment.ca/", status) is not None

    def test_offsite_redirect_is_a_block(self):
        reason = detect_block(PROPERTY_HTML, "https://challenge.example.com/x", 200)
        assert reason is not None and "redirected" in reason

    def test_access_denied_body_is_a_block(self):
        reason = detect_block(BLOCKED_HTML, "https://www.bcassessment.ca/", 200)
        assert reason is not None and "access denied" in reason

    def test_captcha_body_is_a_block(self):
        reason = detect_block(CAPTCHA_HTML, "https://www.bcassessment.ca/", 200)
        assert reason is not None
