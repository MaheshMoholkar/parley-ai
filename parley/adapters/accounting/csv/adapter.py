"""Accounting adapter that reads an aging report CSV and an optional payments CSV.

Invoices file columns (header names are case-insensitive; spaces become "_"):

    invoice_number   required, unique in the file
    customer_id      optional; defaults to customer_name
    customer_name    required
    email, phone     optional
    amount_due       required, e.g. 1234.50 or 1,234.50
    currency         required, e.g. INR
    due_date         required, YYYY-MM-DD
    details          optional text shown in the reminder

An invoice with amount_due 0 counts as paid. An invoice missing from the file
is no longer open, so `get_invoice` returns None for it.

Payments file columns: payment_id, customer_id (or customer_name), amount,
currency, paid_on (YYYY-MM-DD) and an optional reference.

Paths are local files, or "s3://bucket/key" for files in S3 (as on AWS, where
the container has no files of its own). Each sync reads the files again.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import boto3

from parley.core.domain import InvoiceStatus
from parley.core.money import MoneyError, to_minor_units
from parley.ports.accounting import SourceCustomer, SourceInvoice, SourcePayment

INVOICE_COLUMNS = {"invoice_number", "customer_name", "amount_due", "currency", "due_date"}
PAYMENT_COLUMNS = {"payment_id", "amount", "currency", "paid_on"}


class CsvFormatError(ValueError):
    """The file has missing columns or bad rows. Lists every problem found."""

    def __init__(self, path: Path | str, problems: list[str]) -> None:
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: " + "; ".join(problems))


@dataclass
class _Data:
    invoices: dict[str, SourceInvoice] = field(default_factory=dict)
    customers: dict[str, SourceCustomer] = field(default_factory=dict)
    payments: list[SourcePayment] = field(default_factory=list)


class CsvAccountingAdapter:
    source = "csv"

    def __init__(self, invoices_path: Path | str, payments_path: Path | str | None = None) -> None:
        self.invoices_path = _location(invoices_path)
        self.payments_path = _location(payments_path) if payments_path else None
        self._data: _Data | None = None

    # --- AccountingPort methods ----------------------------------------------

    def list_open_invoices(self) -> list[SourceInvoice]:
        return [i for i in self._load().invoices.values() if i.status == InvoiceStatus.OPEN]

    def get_invoice(self, external_id: str) -> SourceInvoice | None:
        return self._load().invoices.get(external_id)

    def get_customer(self, external_id: str) -> SourceCustomer | None:
        return self._load().customers.get(external_id)

    def list_payments_since(
        self, since: date, customer_external_id: str | None = None
    ) -> list[SourcePayment]:
        return [
            p
            for p in self._load().payments
            if p.paid_on >= since
            and (customer_external_id is None or p.customer_external_id == customer_external_id)
        ]

    # --- Reading the files -----------------------------------------------------

    def _load(self) -> _Data:
        # Files are read once per adapter instance; sync creates a new instance
        # each run, so it always sees the latest files.
        if self._data is None:
            data = _Data()
            self._read_invoices(data)
            if self.payments_path is not None:
                self._read_payments(data)
            self._data = data
        return self._data

    def _read_invoices(self, data: _Data) -> None:
        problems: list[str] = []
        for line_no, row in _read_rows(self.invoices_path, INVOICE_COLUMNS):
            try:
                number = _required(row, "invoice_number")
                name = _required(row, "customer_name")
                customer_id = row.get("customer_id") or name
                currency = _required(row, "currency").upper()
                amount = to_minor_units(_required(row, "amount_due"), currency)
                if amount < 0:
                    raise ValueError("amount_due cannot be negative")
                if number in data.invoices:
                    raise ValueError(f"invoice_number {number!r} appears more than once")

                data.invoices[number] = SourceInvoice(
                    external_id=number,
                    customer_external_id=customer_id,
                    number=number,
                    amount_due=amount,
                    currency=currency,
                    due_date=_parse_date(_required(row, "due_date")),
                    status=InvoiceStatus.OPEN if amount > 0 else InvoiceStatus.PAID,
                    display_details=row.get("details", ""),
                )
                _merge_customer(data, customer_id, name, row.get("email"), row.get("phone"))
            except (ValueError, MoneyError) as exc:
                problems.append(f"line {line_no}: {exc}")
        if problems:
            raise CsvFormatError(self.invoices_path, problems)

    def _read_payments(self, data: _Data) -> None:
        assert self.payments_path is not None
        problems: list[str] = []
        for line_no, row in _read_rows(self.payments_path, PAYMENT_COLUMNS):
            try:
                customer_id = row.get("customer_id") or _required(row, "customer_name")
                currency = _required(row, "currency").upper()
                data.payments.append(
                    SourcePayment(
                        external_id=_required(row, "payment_id"),
                        customer_external_id=customer_id,
                        amount=to_minor_units(_required(row, "amount"), currency),
                        currency=currency,
                        paid_on=_parse_date(_required(row, "paid_on")),
                        reference=row.get("reference", ""),
                    )
                )
            except (ValueError, MoneyError) as exc:
                problems.append(f"line {line_no}: {exc}")
        if problems:
            raise CsvFormatError(self.payments_path, problems)


def _location(path: Path | str) -> Path | str:
    """An S3 address stays a string ("s3://..." is not a file path); anything
    else is a local file."""
    text = str(path)
    return text if text.startswith("s3://") else Path(text)


def _read_text(path: Path | str) -> str:
    # "utf-8-sig" also accepts files saved by Excel, which start with a byte-order mark.
    if isinstance(path, str):
        bucket, _, key = path.removeprefix("s3://").partition("/")
        body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        return bytes(body).decode("utf-8-sig")
    return path.read_text(encoding="utf-8-sig")


def _read_rows(path: Path | str, required_columns: set[str]) -> list[tuple[int, dict[str, str]]]:
    """Return (line number, row) pairs with normalised headers and trimmed values."""
    with io.StringIO(_read_text(path), newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            raise CsvFormatError(path, ["file is empty"])
        columns = [h.strip().lower().replace(" ", "_") for h in header]
        missing = required_columns - set(columns)
        if missing:
            raise CsvFormatError(path, [f"missing columns: {', '.join(sorted(missing))}"])

        rows = []
        for line_no, values in enumerate(reader, start=2):
            if not any(v.strip() for v in values):
                continue  # skip blank lines
            row = {col: val.strip() for col, val in zip(columns, values, strict=False)}
            rows.append((line_no, row))
        return rows


def _required(row: dict[str, str], column: str) -> str:
    value = row.get(column, "")
    if not value:
        raise ValueError(f"{column} is empty")
    return value


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a YYYY-MM-DD date") from None


def _merge_customer(
    data: _Data, customer_id: str, name: str, email: str | None, phone: str | None
) -> None:
    """Several rows can name the same customer; the first non-empty value of each field wins."""
    existing = data.customers.get(customer_id)
    if existing is None:
        data.customers[customer_id] = SourceCustomer(
            customer_id, name, email or None, phone or None
        )
        return
    data.customers[customer_id] = SourceCustomer(
        customer_id,
        existing.name,
        existing.email or email or None,
        existing.phone or phone or None,
    )
