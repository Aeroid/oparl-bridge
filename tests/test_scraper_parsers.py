"""Unit tests for scraper helper functions — no browser required."""

import pytest

from oparl_bridge.scraper.base import _extract_int_param, _parse_german_datetime


def test_extract_int_param_silfdnr():
    assert _extract_int_param("si020?SILFDNR=123", "SILFDNR") == 123


def test_extract_int_param_grlfdnr():
    assert _extract_int_param("gr020?GRLFDNR=42&foo=bar", "GRLFDNR") == 42


def test_extract_int_param_missing():
    assert _extract_int_param("gr020?GRLFDNR=42", "SILFDNR") is None


def test_extract_int_param_ampersand_prefix():
    assert _extract_int_param("page?foo=1&SILFDNR=99", "SILFDNR") == 99


def test_parse_german_datetime_full():
    result = _parse_german_datetime("12.06.2024 19:00 Uhr")
    assert result == "2024-06-12T19:00:00"


def test_parse_german_datetime_date_only():
    result = _parse_german_datetime("03.03.2024")
    assert result == "2024-03-03T00:00:00"


def test_parse_german_datetime_none():
    assert _parse_german_datetime(None) is None
    assert _parse_german_datetime("") is None


def test_parse_german_datetime_garbage():
    assert _parse_german_datetime("keine Angabe") is None
