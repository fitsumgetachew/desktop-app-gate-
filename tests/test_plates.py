import pytest

from smart_gate.utils.plates import normalize_plate


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ABC1234", "ABC1234"),
        ("ABC-1234", "ABC1234"),
        ("abc-1234", "ABC1234"),
        (" abc 1234 ", "ABC1234"),
        ("A-B_C.1/2 3 4", "ABC1234"),
        ("3-12345-AA", "312345AA"),
        ("", ""),
        ("   ", ""),
        (None, ""),
        ("---", ""),
    ],
)
def test_normalize_plate(raw, expected):
    assert normalize_plate(raw) == expected


def test_normalize_plate_is_idempotent():
    once = normalize_plate("abc-1234")
    assert normalize_plate(once) == once


def test_ai_and_human_forms_converge():
    """The regression this helper exists for: the ALPR pipeline emits ABC1234
    while the portal stores ABC-1234, so lookups used to miss every time."""
    assert normalize_plate("ABC1234") == normalize_plate("ABC-1234")
