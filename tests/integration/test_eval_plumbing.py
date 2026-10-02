"""The evals run end to end with fake models.

This checks the plumbing, not the model: seeding, the tasks, the scorers and
the gate. An "oracle" model that answers exactly as labelled must score 100%,
and a model that gets promise dates wrong must be blocked by the gate. That is
the mechanism by which CI blocks a pull request that makes reply reading worse.
"""

import json
import re
from datetime import date, timedelta
from typing import Any

from evalkit import Example, check_gate, run_eval
from evals import drafts, investigations, personas, replies
from harness import FinalAnswer, ToolCall
from harness.model import Reply, ToolResult
from harness.testing import ScriptedModel
from parley.adapters.models.fake import FakeCall, FakeModel
from parley.collections_ai.jobs import BriefOut, DraftOut, ReplyReading, ToneVerdict
from parley.core.domain import ReplyIntent
from parley.db.session import SessionFactory

# --- Reply reading -------------------------------------------------------------------------


def reply_oracle(break_dates: bool = False) -> FakeModel:
    """Answers each reply exactly as labelled (or, if asked, with promise dates a day late)."""
    by_text = {e.input["text"]: e.expected for e in replies.load()}

    def respond(call: FakeCall) -> ReplyReading:
        text = call.prompt.split("<reply>\n", 1)[1].rsplit("\n</reply>", 1)[0]
        label = by_text[text]
        promised = label.get("promised_date")
        if promised and break_dates:
            promised = (date.fromisoformat(promised) + timedelta(days=1)).isoformat()
        return ReplyReading(
            intent=ReplyIntent(label["intent"]),
            promised_date=promised,
            promised_amount=label.get("promised_amount"),
            invoice_numbers=label.get("invoice_numbers", []),
            language=label["language"],
            confidence=0.95,
            summary="ok",
        )

    return FakeModel(respond)


def run_replies(model: FakeModel) -> Any:
    examples = [e for e in replies.load() if e.split == "test"]
    return run_eval(
        "replies", examples, replies.make_task(model), replies.SCORERS, replies.COUNT_METRICS
    )


def test_reply_eval_scores_an_oracle_perfectly_and_gates_a_worse_model() -> None:
    good = run_replies(reply_oracle())
    metrics = good.metrics()
    for name in ("intent_correct", "date_correct", "amount_correct", "language_correct"):
        assert metrics[name] == 1.0, name
    assert metrics["injection_failures"] == 0

    baseline = good.metrics()
    assert check_gate(good.metrics(), good.counts(), replies.RULES, baseline).passed

    worse = run_replies(reply_oracle(break_dates=True))
    result = check_gate(worse.metrics(), worse.counts(), replies.RULES, baseline)
    assert not result.passed
    assert any(line.startswith("FAIL date_correct") for line in result.lines)


def test_low_confidence_counts_as_a_miss() -> None:
    def unsure(call: FakeCall) -> ReplyReading:
        return ReplyReading(intent=ReplyIntent.PROMISE, confidence=0.2, summary="?")

    model = FakeModel(unsure)
    example = replies.load()[0]
    output = replies.make_task(model)(example)
    assert output["intent"] == "other"
    assert [c.tier for c in model.calls] == ["small", "large"]


# --- Drafting ------------------------------------------------------------------------------


def faithful_drafter(call: FakeCall) -> DraftOut | ToneVerdict:
    if call.output_type is ToneVerdict:
        return ToneVerdict(passed=True, reason="polite")
    facts = json.loads(call.prompt)
    rows = "\n".join(
        f"Invoice {i['number']}: {i['amount_due']}, due {i['due_date']}." for i in facts["invoices"]
    )
    return DraftOut(subject="Payment reminder", body=f"Dear {facts['customer_name']},\n{rows}")


def test_draft_eval_counts_wrong_amounts() -> None:
    examples = drafts.load()
    good = run_eval(
        "drafts",
        examples,
        drafts.make_task(FakeModel(faithful_drafter)),
        drafts.SCORERS,
        drafts.COUNT_METRICS,
    )
    assert good.metrics()["checks_pass"] == 1.0
    assert good.metrics()["wrong_amount_drafts"] == 0

    def sloppy(call: FakeCall) -> DraftOut | ToneVerdict:
        draft = faithful_drafter(call)
        if isinstance(draft, DraftOut):
            draft = DraftOut(
                subject=draft.subject, body=re.sub(r"INR [\d,]+\.\d\d", "INR 1.00", draft.body)
            )
        return draft

    bad = run_eval(
        "drafts",
        examples,
        drafts.make_task(FakeModel(sloppy)),
        drafts.SCORERS,
        drafts.COUNT_METRICS,
    )
    assert bad.metrics()["wrong_amount_drafts"] == len(examples)
    assert not check_gate(bad.metrics(), bad.counts(), drafts.RULES, None).passed


# --- Investigator ----------------------------------------------------------------------------


def investigation_oracle(example: Example) -> ScriptedModel:
    """Searches the whole year, then cites exactly the labelled payments."""
    expected = example.expected
    wanted = {
        (p["amount"], p["paid_on"], p["reference"])
        for p in example.input["payments"]
        if p["ref"] in (expected["evidence_refs"] or [])
    }

    def submit(received: list[ToolResult | Reply]) -> FinalAnswer:
        rows = json.loads(received[-1].content)  # type: ignore[union-attr]
        rows = rows["rows"] if isinstance(rows, dict) else rows
        ids = [
            r["id"]
            for r in rows
            if (r["amount"].removeprefix("INR ").replace(",", ""), r["paid_on"], r["reference"])
            in wanted
        ]
        return FinalAnswer(
            "f", {"result": expected["result"], "evidence_ids": ids, "summary": "Checked."}
        )

    search = {"from_date": "2025-01-11", "to_date": "2026-01-10", "near_amount": "10000"}
    return ScriptedModel([ToolCall("s", "search_payments", search), submit])


def test_investigation_eval_seeds_runs_and_scores(session_factory: SessionFactory) -> None:
    examples = investigations.load()
    report = run_eval(
        "investigations",
        examples,
        investigations.make_task(session_factory, investigation_oracle),
        investigations.SCORERS,
        investigations.COUNT_METRICS,
    )
    metrics = report.metrics()
    failing = [r.id for r in report.results if not r.scores.get("result_correct") or r.error]
    assert failing == []
    assert metrics["evidence_correct"] == 1.0
    assert metrics["over_step_cap"] == 0
    assert metrics["steps"] == 2.0


# --- Personas ------------------------------------------------------------------------------


def service_and_persona_model() -> FakeModel:
    """Plays the prompt payer and the disputer, and fakes the service's own jobs."""

    def respond(call: FakeCall) -> Any:
        if call.output_type is DraftOut:
            return faithful_drafter(call)
        if call.output_type is ToneVerdict:
            return ToneVerdict(passed=True, reason="ok")
        if call.output_type is BriefOut:
            return BriefOut(brief="Replies quickly.")
        if call.output_type is personas.PersonaReply:
            if "missed the invoice" in call.system:
                return personas.PersonaReply(reply="Sorry, missed it. Will pay in two days.")
            return personas.PersonaReply(reply="The goods arrived damaged. This invoice is wrong.")
        # Reading a reply.
        today = date.fromisoformat(re.search(r"Today's date: (\S+)", call.prompt).group(1))  # type: ignore[union-attr]
        if "two days" in call.prompt:
            return ReplyReading(
                intent=ReplyIntent.PROMISE,
                promised_date=today + timedelta(days=2),
                confidence=0.95,
                summary="Promises to pay.",
            )
        return ReplyReading(intent=ReplyIntent.DISPUTE, confidence=0.95, summary="Disputes.")

    return FakeModel(respond)


def test_persona_eval_plays_out_whole_cases(session_factory: SessionFactory) -> None:
    examples = [e for e in personas.load() if e.id in ("prompt_payer", "disputer")]
    model = service_and_persona_model()
    report = run_eval(
        "personas",
        examples,
        personas.make_task(session_factory, model, lambda _: None),
        personas.SCORERS,
        personas.COUNT_METRICS,
        max_workers=1,
    )
    outputs = {r.id: r.output for r in report.results}
    assert outputs["prompt_payer"]["final_state"] == "closed"
    assert outputs["disputer"]["final_state"] == "needs_human"
    assert "review_dispute" in outputs["disputer"]["tasks"]
    assert report.metrics()["final_state_correct"] == 1.0
    assert report.metrics()["policy_violations"] == 0
