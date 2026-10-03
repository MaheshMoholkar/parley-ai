"""The eval datasets are well formed: enough examples, valid labels, both splits."""

from collections import Counter
from datetime import date
from decimal import Decimal

from evals import drafts, investigations, personas, replies
from parley.core.domain import FindingResult, ReplyIntent


def test_replies_dataset() -> None:
    examples = replies.load()
    assert len(examples) >= 150
    languages = Counter(e.expected["language"] for e in examples)
    assert sum(n for lang, n in languages.items() if lang != "en") >= 30
    assert {e.split for e in examples} == {"dev", "test"}
    assert sum(1 for e in examples if "injection" in e.tags) >= 5
    for e in examples:
        ReplyIntent(e.expected["intent"])
        today = date.fromisoformat(e.input["today"])
        if e.expected["intent"] == "promise":
            assert date.fromisoformat(e.expected["promised_date"]) > today, e.id
            if e.expected["promised_amount"] is not None:
                Decimal(e.expected["promised_amount"])
        assert set(e.expected.get("invoice_numbers", [])) <= set(e.input["invoices"]), e.id


def test_drafts_dataset() -> None:
    examples = drafts.load()
    assert len(examples) >= 20
    for e in examples:
        assert drafts.lines_of(e)


def test_investigations_dataset() -> None:
    examples = investigations.load()
    assert len(examples) >= 25
    for e in examples:
        FindingResult(e.expected["result"])
        refs = {p["ref"] for p in e.input["payments"] if p["customer"] == "self"}
        assert set(e.expected["evidence_refs"] or []) <= refs, e.id


def test_personas_dataset() -> None:
    names = {e.id for e in personas.load()}
    assert names == {
        "prompt_payer",
        "evasive",
        "promise_breaker",
        "disputer",
        "already_paid",
        "hostile",
    }
