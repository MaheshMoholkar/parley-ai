from datetime import date

import pytest

from parley.core.messages import InvoiceLine, reminder_message

INV_1 = InvoiceLine("INV-1", 123450, "INR", date(2026, 1, 1), "Order 77")
INV_2 = InvoiceLine("INV-2", 50000, "INR", date(2026, 1, 3))


def test_single_invoice_reminder_states_amount_and_due_date() -> None:
    subject, body = reminder_message("Asha Stores", "Acme Traders", [INV_1], "friendly")
    assert subject == "Invoice INV-1 from Acme Traders is overdue"
    assert "Dear Asha Stores," in body
    assert "- Invoice INV-1: INR 1,234.50, due 01 Jan 2026 (Order 77)" in body
    assert "Total due" not in body


def test_several_invoices_go_in_one_message_with_a_total() -> None:
    subject, body = reminder_message("Asha Stores", "Acme Traders", [INV_1, INV_2], "firm")
    assert subject == "2 overdue invoices from Acme Traders"
    assert "INV-1" in body and "INV-2" in body
    assert "Total due: INR 1,734.50" in body
    assert "still unpaid" in body


def test_unknown_tone_falls_back_to_friendly() -> None:
    _, body = reminder_message("A", "B", [INV_1], "sarcastic")
    assert "friendly reminder" in body


def test_reminder_needs_an_invoice() -> None:
    with pytest.raises(ValueError):
        reminder_message("A", "B", [], "friendly")
