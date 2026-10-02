"""Checks run on every drafted message before it can be sent (spec: "Output checks").

They are plain code, not a model: a draft that names a wrong amount, a wrong due
date, or a banned phrase is caught here no matter who or what wrote it.
"""

import re
from collections.abc import Sequence
from datetime import date, datetime

from parley.core.messages import InvoiceLine
from parley.core.money import MoneyError, to_minor_units

# Phrases a collections message must never contain: legal threats, public
# shaming, and contacting third parties. Matched as whole words, any case.
BANNED_PHRASES = (
    "legal action",
    "legal proceedings",
    "lawyer",
    "advocate",
    "court",
    "police",
    "arrest",
    "jail",
    "sue you",
    "blacklist",
    "credit score",
    "credit bureau",
    "cibil",
    "shame",
    "public notice",
    "social media",
    "your family",
    "your employer",
    "your neighbours",
    "your neighbors",
)

_BANNED = re.compile(r"\b(" + "|".join(re.escape(p) for p in BANNED_PHRASES) + r")\b", re.I)

# "INR 1,234.50", "Rs. 1234", "₹1,23,450.00", "USD 10"
_CURRENCY_SYMBOLS = {"₹": "INR", "RS": "INR", "RS.": "INR", "$": "USD", "€": "EUR", "£": "GBP"}
_MONEY = re.compile(
    r"(?P<cur>INR|USD|EUR|GBP|AED|SGD|JPY|Rs\.?|₹|\$|€|£)\s?(?P<num>\d[\d,]*(?:\.\d+)?)", re.I
)

# "01 Jan 2026", "1 January 2026", "2026-01-01", "01/01/2026" (day first)
_DATE_PATTERNS = [
    (re.compile(r"\b\d{1,2} [A-Z][a-z]{2,8} \d{4}\b"), ("%d %b %Y", "%d %B %Y")),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), ("%Y-%m-%d",)),
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"), ("%d/%m/%Y",)),
]


def check_draft(subject: str, body: str, lines: Sequence[InvoiceLine]) -> list[str]:
    """Return a list of problems; an empty list means the draft may be sent."""
    problems: list[str] = []
    text = f"{subject}\n{body}"
    if not subject.strip() or not body.strip():
        problems.append("subject or body is empty")

    for match in _BANNED.finditer(text):
        problems.append(f"banned phrase: {match.group(0)!r}")

    problems += _check_amounts(text, lines)
    problems += _check_dates(text, lines)
    for line in lines:
        if line.number not in text:
            problems.append(f"invoice number {line.number} is missing")
    return problems


def _check_amounts(text: str, lines: Sequence[InvoiceLine]) -> list[str]:
    allowed = {(line.currency, line.amount_due) for line in lines}
    currencies = {line.currency for line in lines}
    if len(currencies) == 1:
        currency = next(iter(currencies))
        allowed.add((currency, sum(line.amount_due for line in lines)))

    mentioned: set[tuple[str, int]] = set()
    problems = []
    for match in _MONEY.finditer(text):
        symbol = match.group("cur")
        currency = _CURRENCY_SYMBOLS.get(symbol.upper(), symbol.upper())
        try:
            amount = to_minor_units(match.group("num"), currency)
        except MoneyError:
            problems.append(f"unreadable amount {match.group(0)!r}")
            continue
        if (currency, amount) not in allowed:
            problems.append(f"amount {match.group(0)!r} does not match any amount due")
        mentioned.add((currency, amount))

    for line in lines:
        if (line.currency, line.amount_due) not in mentioned:
            problems.append(f"amount due for invoice {line.number} is missing")
    return problems


def _check_dates(text: str, lines: Sequence[InvoiceLine]) -> list[str]:
    due_dates = {line.due_date for line in lines}
    mentioned: set[date] = set()
    problems = []
    for pattern, formats in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            parsed = _parse_date(match.group(0), formats)
            if parsed is None:
                continue  # looked like a date but is not one, e.g. "12 Apples 2026"
            if parsed not in due_dates:
                problems.append(f"date {match.group(0)!r} is not a due date")
            mentioned.add(parsed)
    for line in lines:
        if line.due_date not in mentioned:
            problems.append(f"due date for invoice {line.number} is missing")
    return problems


def _parse_date(text: str, formats: tuple[str, ...]) -> date | None:
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


MAX_BRIEF_WORDS = 150


def check_brief(text: str) -> list[str]:
    """A customer brief must stay short and must not carry amounts, which always
    come from the books."""
    problems = []
    if len(text.split()) > MAX_BRIEF_WORDS:
        problems.append(f"brief is longer than {MAX_BRIEF_WORDS} words")
    if _MONEY.search(text):
        problems.append("brief contains an amount")
    return problems
