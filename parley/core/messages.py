"""The fixed reminder template used until the model drafter arrives in M2.

Every amount and date comes from the database values passed in, never from free
text, which is the rule the drafter will be checked against later.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from parley.core.money import format_money


@dataclass(frozen=True)
class InvoiceLine:
    number: str
    amount_due: int
    currency: str
    due_date: date
    display_details: str = ""


_OPENING = {
    "friendly": "This is a friendly reminder that the following {noun} {verb} now overdue.",
    "firm": "Our records show the following {noun} {verb} still unpaid past the due date.",
    "final": (
        "This is our final reminder about the following overdue {noun}. "
        "Please pay now or reply to tell us when you will."
    ),
}

_CLOSING = {
    "friendly": "If you have already paid, please reply with the payment details and ignore this.",
    "firm": "Please arrange payment, or reply to let us know when it will be made.",
    "final": "If there is a problem with {these}, reply to this email so we can sort it out.",
}


def reminder_message(
    customer_name: str,
    business_name: str,
    lines: Sequence[InvoiceLine],
    tone: str,
) -> tuple[str, str]:
    """Build (subject, body) for one reminder that may cover several invoices."""
    if not lines:
        raise ValueError("a reminder needs at least one invoice")
    tone = tone if tone in _OPENING else "friendly"
    many = len(lines) > 1
    noun = "invoices" if many else "invoice"

    if many:
        subject = f"{len(lines)} overdue invoices from {business_name}"
    else:
        subject = f"Invoice {lines[0].number} from {business_name} is overdue"

    rows = []
    for line in lines:
        row = (
            f"- Invoice {line.number}: {format_money(line.amount_due, line.currency)}, "
            f"due {line.due_date:%d %b %Y}"
        )
        if line.display_details:
            row += f" ({line.display_details})"
        rows.append(row)

    currencies = {line.currency for line in lines}
    total = ""
    if many and len(currencies) == 1:
        total_minor = sum(line.amount_due for line in lines)
        total = f"\nTotal due: {format_money(total_minor, currencies.pop())}\n"

    opening = _OPENING[tone].format(noun=noun, verb="are" if many else "is")
    closing = _CLOSING[tone].format(these="these invoices" if many else "this invoice")
    body = (
        f"Dear {customer_name},\n\n"
        f"{opening}\n\n" + "\n".join(rows) + "\n" + total + f"\n{closing}\n\n"
        f"Regards,\n{business_name}\n"
    )
    return subject, body
