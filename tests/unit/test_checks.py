from datetime import date

import pytest

from parley.core.checks import check_draft
from parley.core.messages import InvoiceLine, reminder_message

LINE_1 = InvoiceLine("INV-1", 123450, "INR", date(2026, 1, 1))
LINE_2 = InvoiceLine("INV-2", 50000, "INR", date(2026, 1, 3))
GOOD_BODY = (
    "Dear Asha,\n\nInvoice INV-1 for INR 1,234.50 was due on 01 Jan 2026 and "
    "invoice INV-2 for INR 500.00 was due on 03 Jan 2026. Total due: INR 1,734.50.\n"
)


def test_a_correct_draft_passes() -> None:
    assert check_draft("Two overdue invoices", GOOD_BODY, [LINE_1, LINE_2]) == []


def test_the_fixed_template_always_passes() -> None:
    subject, body = reminder_message("Asha", "Acme", [LINE_1, LINE_2], "final")
    assert check_draft(subject, body, [LINE_1, LINE_2]) == []


@pytest.mark.parametrize(
    "text",
    ["₹1,234.50", "Rs. 1234.50", "Rs 1,234.5", "INR 1234.50", "inr 1,234.50"],
)
def test_amount_formats_are_recognised(text: str) -> None:
    body = f"Invoice INV-1 for {text} was due on 01 Jan 2026."
    assert check_draft("Reminder", body, [LINE_1]) == []


def test_indian_digit_grouping_is_read_correctly() -> None:
    line = InvoiceLine("INV-9", 12345000, "INR", date(2026, 1, 1))
    assert check_draft("x", "Invoice INV-9: INR 1,23,450.00 due 01 Jan 2026", [line]) == []


def test_a_wrong_amount_is_blocked() -> None:
    body = GOOD_BODY.replace("INR 500.00", "INR 550.00")
    problems = check_draft("Reminder", body, [LINE_1, LINE_2])
    assert "amount 'INR 550.00' does not match any amount due" in problems
    assert "amount due for invoice INV-2 is missing" in problems


def test_a_wrong_or_missing_due_date_is_blocked() -> None:
    problems = check_draft(
        "Reminder", GOOD_BODY.replace("03 Jan 2026", "13 Jan 2026"), [LINE_1, LINE_2]
    )
    assert "date '13 Jan 2026' is not a due date" in problems
    assert "due date for invoice INV-2 is missing" in problems


def test_other_date_formats_are_checked() -> None:
    body = "Invoice INV-1 for INR 1,234.50 was due on 2026-01-02."
    assert "date '2026-01-02' is not a due date" in check_draft("x", body, [LINE_1])


def test_a_missing_invoice_number_is_blocked() -> None:
    body = "Your invoice for INR 1,234.50 was due on 01 Jan 2026."
    assert check_draft("x", body, [LINE_1]) == ["invoice number INV-1 is missing"]


@pytest.mark.parametrize(
    "phrase", ["legal action", "Court", "the POLICE", "your employer", "credit score"]
)
def test_banned_phrases_are_blocked(phrase: str) -> None:
    problems = check_draft(
        "x", GOOD_BODY + f"\nOtherwise we will involve {phrase}.", [LINE_1, LINE_2]
    )
    assert any(p.startswith("banned phrase") for p in problems)


def test_words_that_merely_contain_a_banned_word_pass() -> None:
    assert check_draft("x", GOOD_BODY + "\nThank you for your courtesy.", [LINE_1, LINE_2]) == []


def test_empty_draft_is_blocked() -> None:
    assert "subject or body is empty" in check_draft("", "", [LINE_1])
