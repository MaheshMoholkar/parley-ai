"""Drafting eval: does the model's reminder pass the code checks and the tone judge?

Wrong-amount and banned-phrase drafts are counts gated at zero: the production
code would catch them, but a draft that needs catching costs a retry or a
person's time, so the prompt must not produce them.
"""

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from evalkit import Example, Rule, Scorer, load_jsonl
from parley.collections_ai.jobs import DraftRequest, draft_reminder, judge_tone
from parley.core.checks import check_draft
from parley.core.messages import InvoiceLine
from parley.core.money import to_minor_units
from parley.ports.model import ModelPort

DATASET = Path(__file__).parent / "datasets" / "drafts.jsonl"
RULES = [
    Rule("wrong_amount_drafts", "max", 0),
    Rule("banned_phrase_drafts", "max", 0),
    Rule("tone_pass", "no_drop"),
    Rule("checks_pass", "no_drop"),
]
COUNT_METRICS = frozenset({"wrong_amount_drafts", "banned_phrase_drafts"})


def load() -> list[Example]:
    return load_jsonl(DATASET)


def lines_of(example: Example) -> list[InvoiceLine]:
    return [
        InvoiceLine(
            number=i["number"],
            amount_due=to_minor_units(i["amount_due"], i["currency"]),
            currency=i["currency"],
            due_date=date.fromisoformat(i["due_date"]),
            display_details=i.get("details", ""),
        )
        for i in example.input["invoices"]
    ]


def make_task(model: ModelPort) -> Callable[[Example], dict[str, Any]]:
    def task(example: Example) -> dict[str, Any]:
        lines = lines_of(example)
        request = DraftRequest(
            business_name=example.input["business_name"],
            customer_name=example.input["customer_name"],
            language=example.input["language"],
            tone=example.input["tone"],
            customer_brief=example.input["customer_brief"],
            lines=lines,
            payment_link=example.input.get("payment_link"),
        )
        draft = draft_reminder(model, request)[0].output
        problems = check_draft(draft.subject, draft.body, lines)
        verdict = judge_tone(model, request.tone, draft.subject, draft.body)[0].output
        return {
            "subject": draft.subject,
            "body": draft.body,
            "problems": problems,
            "tone_passed": verdict.passed,
            "tone_reason": verdict.reason,
        }

    return task


def score(example: Example, output: dict[str, Any]) -> dict[str, float | bool | int]:
    problems: list[str] = output["problems"]
    facts = [p for p in problems if not p.startswith("banned phrase")]
    return {
        "checks_pass": not problems,
        "wrong_amount_drafts": int(bool(facts)),
        "banned_phrase_drafts": int(any(p.startswith("banned phrase") for p in problems)),
        "tone_pass": bool(output["tone_passed"]),
    }


SCORERS: list[Scorer] = [score]
