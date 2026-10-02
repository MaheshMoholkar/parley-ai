"""The model jobs. Each is one call to the model port with a typed output.

These functions only build the prompt and make the call. Checking the output
and acting on it is done by code in the services layer, never by the model.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from parley.collections_ai.prompts import Prompt, load_prompt
from parley.core.domain import ReplyIntent
from parley.core.messages import InvoiceLine
from parley.core.money import format_money
from parley.ports.model import Completion, ModelPort

DRAFT_PROMPT = "draft_reminder.v1"
TONE_PROMPT = "tone_judge.v1"
READ_PROMPT = "read_reply.v1"

Language = Literal["en", "hi", "mr", "hinglish"]


# --- Drafting a reminder ---------------------------------------------------------


class DraftOut(BaseModel):
    subject: str = Field(description="Email subject line")
    body: str = Field(description="Plain-text email body")


@dataclass(frozen=True)
class DraftRequest:
    business_name: str
    customer_name: str
    language: str
    tone: str
    customer_brief: str
    lines: Sequence[InvoiceLine]
    payment_link: str | None = None


def draft_reminder(model: ModelPort, request: DraftRequest) -> tuple[Completion[DraftOut], Prompt]:
    prompt = load_prompt(DRAFT_PROMPT)
    facts: dict[str, object] = {
        "business_name": request.business_name,
        "customer_name": request.customer_name,
        "language": request.language,
        "tone": request.tone,
        "invoices": [
            {
                "number": line.number,
                "amount_due": format_money(line.amount_due, line.currency),
                "due_date": f"{line.due_date:%d %b %Y}",
                "details": line.display_details,
            }
            for line in request.lines
        ],
        "customer_brief": request.customer_brief,
    }
    currencies = {line.currency for line in request.lines}
    if len(request.lines) > 1 and len(currencies) == 1:
        total = sum(line.amount_due for line in request.lines)
        facts["total"] = format_money(total, currencies.pop())
    if request.payment_link:
        facts["payment_link"] = request.payment_link

    # sort_keys keeps the text identical for identical facts.
    user = json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True)
    return model.complete("large", prompt.text, user, DraftOut), prompt


# --- Judging tone ------------------------------------------------------------------


class ToneVerdict(BaseModel):
    passed: bool
    reason: str = Field(description="One line explaining the verdict")


def judge_tone(
    model: ModelPort, tone: str, subject: str, body: str
) -> tuple[Completion[ToneVerdict], Prompt]:
    prompt = load_prompt(TONE_PROMPT)
    user = f"Intended tone: {tone}\n\n<email>\nSubject: {subject}\n\n{body}\n</email>"
    return model.complete("small", prompt.text, user, ToneVerdict), prompt


# --- Reading a reply -----------------------------------------------------------------


class ReplyReading(BaseModel):
    intent: ReplyIntent
    promised_date: date | None = None
    promised_amount: str | None = Field(default=None, description='Plain number, e.g. "4000.00"')
    invoice_numbers: list[str] = Field(default_factory=list)
    language: Language = "en"
    confidence: float = Field(ge=0, le=1)
    summary: str


def read_reply(
    model: ModelPort,
    tier: Literal["small", "large"],
    today: date,
    invoice_numbers: Sequence[str],
    reply_text: str,
) -> tuple[Completion[ReplyReading], Prompt]:
    prompt = load_prompt(READ_PROMPT)
    user = (
        f"Today's date: {today:%Y-%m-%d} ({today:%A})\n"
        f"The reminder was about invoices: {', '.join(invoice_numbers)}\n\n"
        f"<reply>\n{reply_text}\n</reply>"
    )
    return model.complete(tier, prompt.text, user, ReplyReading), prompt
