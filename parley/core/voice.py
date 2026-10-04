"""Rules for phone calls that are checked after the call (spec: "Voice rules").

Speech cannot be reviewed before it is said, so each call's transcript is
audited when the call ends. A call that breaks a rule goes to a person.

The audit reads the call as a list of turns in the order they happened: what
the agent said, what the customer said, and each tool the agent used.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from parley.core.checks import find_banned_phrases
from parley.core.messages import InvoiceLine
from parley.core.money import MoneyError, to_minor_units


@dataclass(frozen=True)
class Turn:
    role: Literal["agent", "customer", "tool"]
    text: str
    # For tool turns: the tool's name, and whether code accepted the call.
    tool: str = ""
    ok: bool = False


# The agent must say it is an AI assistant. These markers cover English, Hindi
# and Hinglish phrasings ("AI assistant", "automated call", "एआई सहायक").
_DISCLOSURE = re.compile(
    r"\bA\.?I\b|artificial intelligence|automated|virtual assistant|एआई|ए\.?आई|कृत्रिम", re.I
)

# Amounts as speech is transcribed, in any currency: "INR 1,000", "₹1000",
# "$400", "1,000 rupees", "400 dollars", "1000 रुपये".
_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_CODES = {
    "INR": "INR", "RS": "INR", "RS.": "INR", "₹": "INR",
    "USD": "USD", "$": "USD", "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP",
    "AED": "AED", "SGD": "SGD", "JPY": "JPY", "¥": "JPY",
    "RUPEE": "INR", "RUPEES": "INR", "RUPAYE": "INR", "RUPAY": "INR",
    "रुपये": "INR", "रुपए": "INR", "रुपया": "INR",
    "DOLLAR": "USD", "DOLLARS": "USD", "EURO": "EUR", "EUROS": "EUR",
    "POUND": "GBP", "POUNDS": "GBP", "DIRHAM": "AED", "DIRHAMS": "AED", "YEN": "JPY",
}  # fmt: skip
_PREFIX = r"INR|USD|EUR|GBP|AED|SGD|JPY|Rs\.?|₹|\$|€|£|¥"
_SUFFIX = (
    r"INR|USD|EUR|GBP|AED|SGD|JPY|rupees?|rupaye|rupay|रुपये|रुपए|रुपया"
    r"|dollars?|euros?|pounds?|dirhams?|yen"
)
_SPOKEN_MONEY = re.compile(
    rf"(?:(?P<pc>{_PREFIX})\s?(?P<a>{_NUMBER}))|(?:(?P<b>{_NUMBER})\s?(?P<sc>{_SUFFIX})\b)",
    re.I,
)
# Before the identity check no amount may be said at all, in figures or words.
_LONG_NUMBER = re.compile(r"\d[\d,]{2,}")
_AMOUNT_WORDS = re.compile(
    r"\b(?:hundred|thousand|lakhs?|lacs?|crores?|million|hazaa?r|sau)\b|हज़ार|हजार|लाख|सौ|करोड़",
    re.I,
)


def audit_call(
    turns: Sequence[Turn], lines: Sequence[InvoiceLine], promised_amounts: Sequence[int] = ()
) -> list[str]:
    """Return the voice rules the call broke; an empty list means it passed.

    `lines` are the invoices the call was about (with the amounts due at the
    time), and `promised_amounts` the amounts of promises logged on the call,
    which the agent may repeat back to the customer.
    """
    agent_turns = [t for t in turns if t.role == "agent" and t.text.strip()]
    if not agent_turns:
        return []  # nobody spoke (for example the line dropped at once)

    problems: list[str] = []
    if not _DISCLOSURE.search(agent_turns[0].text):
        problems.append("the agent did not say it is an AI assistant in its first turn")

    confirmed = False
    for turn in turns:
        if turn.role == "tool" and turn.tool == "confirm_identity" and turn.ok:
            confirmed = True
        if turn.role != "agent":
            continue
        if not confirmed and _mentions_amount(turn.text):
            problems.append(f"an amount was said before the identity check: {turn.text[:80]!r}")
        for phrase in find_banned_phrases(turn.text):
            problems.append(f"banned phrase: {phrase!r}")

    # Amounts the agent may say: what is owed (each invoice, and the total when
    # they share a currency), and promises logged on the call.
    allowed = {(line.currency, line.amount_due) for line in lines}
    allowed |= {(line.currency, amount) for line in lines for amount in promised_amounts}
    currencies = {line.currency for line in lines}
    if len(currencies) == 1:
        allowed.add((next(iter(currencies)), sum(line.amount_due for line in lines)))
    for turn in agent_turns:
        for match in _SPOKEN_MONEY.finditer(turn.text):
            code = (match.group("pc") or match.group("sc")).upper()
            currency = _CODES.get(code, code)
            try:
                amount = to_minor_units((match.group("a") or match.group("b")), currency)
            except MoneyError:
                amount = -1
            if (currency, amount) not in allowed:
                problems.append(f"the agent said an amount that is not owed: {match.group(0)!r}")
    return problems


def _mentions_amount(text: str) -> bool:
    return bool(
        _SPOKEN_MONEY.search(text) or _LONG_NUMBER.search(text) or _AMOUNT_WORDS.search(text)
    )
