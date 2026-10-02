from decimal import Decimal

import pytest

from parley.core.money import MoneyError, format_money, to_minor_units


@pytest.mark.parametrize(
    ("text", "currency", "expected"),
    [
        ("1234.50", "INR", 123450),
        ("1,234.5", "INR", 123450),
        (" 10 ", "INR", 1000),
        ("0", "INR", 0),
        ("500", "JPY", 500),
        (Decimal("0.01"), "USD", 1),
    ],
)
def test_to_minor_units(text: str, currency: str, expected: int) -> None:
    assert to_minor_units(text, currency) == expected


@pytest.mark.parametrize(
    ("text", "currency"),
    [
        ("12.345", "INR"),  # too many decimal places: never round silently
        ("1.5", "JPY"),
        ("abc", "INR"),
        ("NaN", "INR"),
        ("10", "XYZ"),
    ],
)
def test_to_minor_units_rejects_bad_input(text: str, currency: str) -> None:
    with pytest.raises(MoneyError):
        to_minor_units(text, currency)


def test_format_money() -> None:
    assert format_money(123450, "INR") == "INR 1,234.50"
    assert format_money(500, "JPY") == "JPY 500"
