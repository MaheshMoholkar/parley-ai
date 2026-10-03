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

# Amounts as speech is transcribed: "INR 1,000", "₹1000", "1,000 rupees",
# "1000 रुपये". A plain number of three or more digits also counts as an amount
# when checking that nothing was said before the customer's identity was confirmed.
_SPOKEN_MONEY = re.compile(
    r"(?:(?:INR|Rs\.?|₹)\s?(?P<a>\d[\d,]*(?:\.\d+)?))"
    r"|(?:(?P<b>\d[\d,]*(?:\.\d+)?)\s?(?:rupees?|rupaye|rupay|रुपये|रुपए|रुपया))",
    re.I,
)
_LONG_NUMBER = re.compile(r"\d[\d,]{2,}")


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
        if not confirmed and (_SPOKEN_MONEY.search(turn.text) or _LONG_NUMBER.search(turn.text)):
            problems.append(f"an amount was said before the identity check: {turn.text[:80]!r}")
        for phrase in find_banned_phrases(turn.text):
            problems.append(f"banned phrase: {phrase!r}")

    allowed = {line.amount_due for line in lines} | set(promised_amounts)
    if len({line.currency for line in lines}) == 1:
        allowed.add(sum(line.amount_due for line in lines))
    currency = lines[0].currency if lines else "INR"
    for turn in agent_turns:
        for match in _SPOKEN_MONEY.finditer(turn.text):
            text = (match.group("a") or match.group("b")).replace(",", "")
            try:
                amount = to_minor_units(text, currency)
            except MoneyError:
                continue
            if amount not in allowed:
                problems.append(f"the agent said an amount that is not owed: {match.group(0)!r}")
    return problems
