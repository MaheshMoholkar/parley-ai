"""Money is stored as a whole number of minor units (paise for INR) plus a currency code.

Whole numbers avoid floating-point rounding errors: 0.1 + 0.2 is not exactly 0.3
as a float, but 10 + 20 paise is always exactly 30 paise.
"""

from decimal import Decimal, InvalidOperation

# How many decimal places each supported currency uses.
MINOR_UNITS: dict[str, int] = {
    "INR": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "AED": 2,
    "SGD": 2,
    "JPY": 0,
}


class MoneyError(ValueError):
    """An amount or currency that cannot be stored safely."""


def minor_digits(currency: str) -> int:
    try:
        return MINOR_UNITS[currency]
    except KeyError:
        raise MoneyError(f"unsupported currency: {currency!r}") from None


def to_minor_units(amount: str | Decimal, currency: str) -> int:
    """Convert "1,234.50" INR to 123450.

    Rejects amounts with more decimal places than the currency allows, instead of
    silently rounding them.
    """
    digits = minor_digits(currency)
    text = str(amount).replace(",", "").strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise MoneyError(f"not a number: {amount!r}") from None
    if not value.is_finite():
        raise MoneyError(f"not a finite number: {amount!r}")

    scaled = value.scaleb(digits)
    if scaled != scaled.to_integral_value():
        raise MoneyError(f"{amount!r} has more than {digits} decimal places for {currency}")
    return int(scaled)


def format_money(minor: int, currency: str) -> str:
    """Format 123450 INR as "INR 1,234.50"."""
    digits = minor_digits(currency)
    value = Decimal(minor).scaleb(-digits)
    return f"{currency} {value:,.{digits}f}"
