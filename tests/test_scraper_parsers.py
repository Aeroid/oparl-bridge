"""Unit tests for scraper helper functions — no browser required."""

from oparl_bridge.scraper.base import _derive_result, _extract_int_param, _parse_german_datetime


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


def test_parse_german_datetime_weekday_prefix():
    # si018 column 0 includes weekday abbreviation, e.g. "Do.,\n24.09.2026"
    result = _parse_german_datetime("Do., 24.09.2026 19:30")
    assert result == "2026-09-24T19:30:00"


# ---------------------------------------------------------------------------
# _derive_result
# ---------------------------------------------------------------------------

def test_derive_result_accepted():
    assert _derive_result("einstimmig beschlossen", None) == "ACCEPTED"
    assert _derive_result(None, "angenommen") == "ACCEPTED"
    assert _derive_result("beschlossen", None) == "ACCEPTED"


def test_derive_result_rejected():
    assert _derive_result("abgelehnt", None) == "REJECTED"
    assert _derive_result(None, "abgewiesen") == "REJECTED"


def test_derive_result_deferred():
    assert _derive_result("vertagt", None) == "DEFERRED"
    assert _derive_result(None, "zurückgestellt") == "DEFERRED"
    assert _derive_result("verschoben", None) == "DEFERRED"


def test_derive_result_nodecision():
    assert _derive_result("zur kenntnis genommen", None) == "NODECISION"
    assert _derive_result(None, "kenntnisnahme") == "NODECISION"
    assert _derive_result("ohne abstimmung", None) == "NODECISION"


def test_derive_result_none_when_empty():
    assert _derive_result(None, None) is None
    assert _derive_result("", "") is None


def test_derive_result_none_when_unknown():
    assert _derive_result("irgendein Text", "ohne klaren Beschluss") is None
