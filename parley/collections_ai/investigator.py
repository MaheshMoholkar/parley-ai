"""The investigator's prompt, task text and finding (spec: "Investigator harness").

The loop itself is the generic `harness` package; the tools are in
parley/services/investigation.py because they read the database.
"""

from datetime import date

from pydantic import BaseModel, Field

from parley.collections_ai.prompts import Prompt, load_prompt
from parley.core.domain import FindingResult, ReplyIntent
from parley.core.messages import InvoiceLine
from parley.core.money import format_money

INVESTIGATE_PROMPT = "investigate.v1"
FINAL_TOOL = "submit_finding"


class Finding(BaseModel):
    result: FindingResult
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Ids of the payment, invoice or message records the finding relies on",
    )
    summary: str = Field(description="Two or three sentences for the person who picks this up")


def investigation_prompt() -> Prompt:
    return load_prompt(INVESTIGATE_PROMPT)


def investigation_task(
    claim: ReplyIntent, customer_name: str, invoice: InvoiceLine, claim_text: str, today: date
) -> str:
    kind = (
        "says the invoice is already paid"
        if claim == ReplyIntent.PAID_CLAIM
        else "disputes the invoice"
    )
    return (
        f"Today is {today:%d %b %Y}. The customer {customer_name} {kind}.\n\n"
        f"Invoice {invoice.number}: {format_money(invoice.amount_due, invoice.currency)} "
        f"due, due date {invoice.due_date:%d %b %Y}.\n\n"
        f"The customer's message:\n<customer_message>\n{claim_text}\n</customer_message>"
    )
