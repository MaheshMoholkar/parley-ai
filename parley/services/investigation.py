"""The worker step that runs queued investigations (spec: "Investigator harness").

For each queued run: the investigator agent works through its read-only tools,
then submits a finding. Code checks that every evidence id exists for this
tenant and customer, and turns the finding into a task for a person. The model
never changes a case, an invoice or a payment.

Without an agent model configured, the run ends at once as "unclear" and a
person takes the claim, as before the investigator existed.
"""

import logging
import uuid
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from harness import RunResult, Tool, ToolError, run_agent
from parley.collections_ai.investigator import (
    FINAL_TOOL,
    INVESTIGATE_PROMPT,
    Finding,
    investigation_prompt,
    investigation_task,
)
from parley.core.domain import AgentRunStatus, CaseState, Direction, FindingResult
from parley.core.money import MoneyError, format_money, to_minor_units
from parley.core.workflow import on_investigation_done
from parley.db.models import (
    AgentRun,
    Case,
    Customer,
    Invoice,
    Message,
    MessageCase,
    Payment,
    Promise,
    Tenant,
)
from parley.ports.accounting import AccountingPort
from parley.ports.model import TokenUsage
from parley.services.cases import apply_transition, invoice_line
from parley.services.model_calls import estimate_cost_micro_usd
from parley.services.runtime import Runtime
from parley.services.tracing import annotate, step

log = logging.getLogger(__name__)

MAX_SEARCH_DAYS = 366
MAX_MESSAGE_CHARS = 1500


def run_investigations(rt: Runtime, tenant_id: uuid.UUID) -> int:
    """Run every queued investigation of the tenant. Returns how many ran.

    Each run holds a lock on its row and its case for its duration (at most the
    harness time limit), so two workers never investigate the same claim.
    """
    done = 0
    tried: set[uuid.UUID] = set()
    while True:
        with rt.session_factory.begin() as session:
            query = (
                select(AgentRun)
                .where(AgentRun.tenant_id == tenant_id, AgentRun.status == AgentRunStatus.QUEUED)
                .order_by(AgentRun.created_at, AgentRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if tried:
                query = query.where(AgentRun.id.not_in(tried))
            run = session.scalar(query)
            if run is None:
                return done
            tried.add(run.id)
            with step(
                "investigate",
                tenant_id=tenant_id,
                agent_run_id=run.id,
                case_ids=[run.case_id],
                claim=run.claim,
            ) as span:
                _investigate(rt, session, run)
                annotate(
                    span, outcome=run.outcome, result=run.result, cost_micro_usd=run.cost_micro_usd
                )
            done += 1


def _investigate(rt: Runtime, session: Session, run: AgentRun) -> None:
    now = rt.clock.now()
    case = session.get_one(Case, run.case_id, with_for_update=True)
    if case.state != CaseState.INVESTIGATING:
        run.status = AgentRunStatus.CANCELLED
        run.finished_at = now
        return

    tenant = session.get_one(Tenant, run.tenant_id)
    customer = session.get_one(Customer, case.customer_id)
    claim_text = session.get_one(Message, run.message_id).body if run.message_id else ""

    if rt.agent_model is None:
        run.outcome = "no_model"
        result, summary = FindingResult.UNCLEAR, "No investigator model is configured."
    else:
        tools = investigation_tools(session, tenant, customer, case, rt.accounting_for(tenant))
        prompt = investigation_prompt()
        task = investigation_task(
            run.claim,
            customer.name,
            invoice_line(case.invoice),
            claim_text,
            now.astimezone(tenant.zone).date(),
        )
        outcome = run_agent(
            rt.agent_model,
            prompt.text,
            task,
            tools,
            Finding,
            final_name=FINAL_TOOL,
            final_description="Report your finding. Call this once, at the end.",
            limits=rt.agent_limits,
        )
        _record(rt, run, outcome)
        result, summary = _checked_finding(session, run, customer, case, outcome)

    run.result = result
    run.status = AgentRunStatus.DONE
    run.finished_at = now
    transition = on_investigation_done(case.view(), run.claim, result, summary)
    if transition is not None:
        apply_transition(session, case, transition, now, agent_run_id=run.id)


def _record(rt: Runtime, run: AgentRun, outcome: RunResult[Finding]) -> None:
    usage = outcome.usage
    run.outcome = outcome.outcome
    run.steps = [step.as_dict() for step in outcome.steps]
    run.finding = outcome.final.model_dump(mode="json") if outcome.final else None
    run.model = outcome.model_id or None
    run.prompt_version = INVESTIGATE_PROMPT
    run.input_tokens = usage.input_tokens
    run.output_tokens = usage.output_tokens
    run.error = outcome.error or None
    run.cost_micro_usd = estimate_cost_micro_usd(
        rt,
        outcome.model_id,
        TokenUsage(
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        ),
    )


def _checked_finding(
    session: Session, run: AgentRun, customer: Customer, case: Case, outcome: RunResult[Finding]
) -> tuple[FindingResult, str]:
    """Code's check of the model's finding. Anything that does not hold up
    becomes "unclear", with the reason, for a person to pick up."""
    finding = outcome.final
    if finding is None:
        reasons = {
            "step_limit": "it used all its steps without reaching a finding",
            "time_limit": "it ran out of time",
            "model_error": f"the model failed ({outcome.error})",
        }
        return (
            FindingResult.UNCLEAR,
            f"The investigation stopped: {reasons.get(outcome.outcome, outcome.outcome)}.",
        )

    evidence, unknown = describe_evidence(session, run.tenant_id, customer.id, finding.evidence_ids)
    if unknown:
        return FindingResult.UNCLEAR, (
            f"The investigator cited records that do not exist ({', '.join(unknown)}), "
            f"so its finding was set aside. It said: {finding.summary}"
        )
    needs_payment = finding.result in (FindingResult.PAYMENT_FOUND, FindingResult.PARTIAL_PAYMENT)
    if needs_payment and not any(kind == "payment" for kind, _ in evidence):
        return FindingResult.UNCLEAR, (
            f"The investigator reported {finding.result} without citing a payment. "
            f"It said: {finding.summary}"
        )
    if needs_payment:
        problem = _payment_evidence_problem(session, case, finding)
        if problem:
            return FindingResult.UNCLEAR, (
                f"The payments the investigator cited do not support {finding.result}: "
                f"{problem}. It said: {finding.summary}"
            )
    cited = "; ".join(text for _, text in evidence)
    return finding.result, f"{finding.summary} Evidence: {cited or 'none cited'}."


# A payment made this long before the due date cannot be for this invoice.
MAX_PAYMENT_LEAD = timedelta(days=120)


def _payment_evidence_problem(session: Session, case: Case, finding: Finding) -> str:
    """Check the cited payments can pay this invoice: same currency, not long
    before it was due, and (for "payment found") enough to cover it. A person
    still confirms; this only stops a misleading finding reaching them."""
    ids = []
    for raw in finding.evidence_ids:
        try:
            ids.append(uuid.UUID(raw))
        except ValueError:
            continue
    payments = session.scalars(
        select(Payment).where(Payment.id.in_(ids), Payment.customer_id == case.customer_id)
    ).all()
    invoice = case.invoice
    for payment in payments:
        if payment.currency != invoice.currency:
            return f"a payment is in {payment.currency}, the invoice in {invoice.currency}"
        if payment.paid_on < invoice.due_date - MAX_PAYMENT_LEAD:
            return f"a payment of {payment.paid_on:%d %b %Y} is long before the invoice was due"
    total = sum(payment.amount for payment in payments)
    if finding.result == FindingResult.PAYMENT_FOUND and total < invoice.amount_due:
        return "they add up to less than the amount due"
    return ""


def describe_evidence(
    session: Session, tenant_id: uuid.UUID, customer_id: uuid.UUID, ids: list[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Look up each cited id among this customer's payments, invoices and
    messages. Returns ([(kind, description)], [ids that were not found])."""
    found: list[tuple[str, str]] = []
    unknown: list[str] = []
    for raw in ids:
        try:
            record_id = uuid.UUID(raw)
        except ValueError:
            unknown.append(raw)
            continue
        payment = session.scalar(
            select(Payment).where(
                Payment.id == record_id,
                Payment.tenant_id == tenant_id,
                Payment.customer_id == customer_id,
            )
        )
        if payment is not None:
            found.append(("payment", _describe_payment(payment)))
            continue
        invoice = session.scalar(
            select(Invoice).where(
                Invoice.id == record_id,
                Invoice.tenant_id == tenant_id,
                Invoice.customer_id == customer_id,
            )
        )
        if invoice is not None:
            found.append(("invoice", f"invoice {invoice.number}"))
            continue
        message = session.scalar(
            select(Message).where(
                Message.id == record_id,
                Message.tenant_id == tenant_id,
                Message.customer_id == customer_id,
            )
        )
        if message is not None:
            found.append(
                ("message", f"{message.direction} message of {message.created_at:%d %b %Y}")
            )
            continue
        unknown.append(raw)
    return found, unknown


def _describe_payment(payment: Payment) -> str:
    text = (
        f"payment of {format_money(payment.amount, payment.currency)} on {payment.paid_on:%d %b %Y}"
    )
    return text + (f" (ref {payment.reference})" if payment.reference else "")


# --- The four tools -------------------------------------------------------------------
# Each is read-only and sees only this case's customer, whatever the model asks.


class GetInvoiceIn(BaseModel):
    invoice_number: str = Field(description="The invoice number, e.g. INV-1042")


class SearchPaymentsIn(BaseModel):
    from_date: date = Field(description="First payment date to include (YYYY-MM-DD)")
    to_date: date = Field(description="Last payment date to include (YYYY-MM-DD)")
    near_amount: str | None = Field(
        default=None, description='Optional amount to sort by closeness, e.g. "4000.00"'
    )

    @model_validator(mode="after")
    def _check_range(self) -> "SearchPaymentsIn":
        if self.to_date < self.from_date:
            raise ValueError("to_date is before from_date")
        if (self.to_date - self.from_date) > timedelta(days=MAX_SEARCH_DAYS):
            raise ValueError(f"search at most {MAX_SEARCH_DAYS} days at a time")
        return self


class NoInput(BaseModel):
    pass


def investigation_tools(
    session: Session,
    tenant: Tenant,
    customer: Customer,
    case: Case,
    accounting: AccountingPort,
) -> list[Tool]:
    def get_invoice(args: GetInvoiceIn) -> dict[str, Any]:
        invoice = session.scalar(
            select(Invoice).where(
                Invoice.tenant_id == tenant.id,
                Invoice.customer_id == customer.id,
                Invoice.number == args.invoice_number,
            )
        )
        if invoice is None:
            raise ToolError(f"This customer has no invoice numbered {args.invoice_number!r}.")
        result: dict[str, Any] = {
            "id": str(invoice.id),
            "number": invoice.number,
            "amount_due": format_money(invoice.amount_due, invoice.currency),
            "due_date": invoice.due_date.isoformat(),
            "status": str(invoice.status),
            "credit_notes": "Not available from this accounting source.",
        }
        # Ask the source for the latest figures; fall back to the last sync.
        try:
            latest = accounting.get_invoice(invoice.external_id)
        except Exception as exc:
            log.warning("source lookup of invoice %s failed: %s", invoice.number, exc)
            result["note"] = "Live figures unavailable; showing the last sync."
        else:
            if latest is None:
                result["status"] = "no longer in the accounting system"
            else:
                result["amount_due"] = format_money(latest.amount_due, latest.currency)
                result["status"] = str(latest.status)
        return result

    def search_payments(args: SearchPaymentsIn) -> list[dict[str, Any]]:
        payments = list(
            session.scalars(
                select(Payment)
                .where(
                    Payment.tenant_id == tenant.id,
                    Payment.customer_id == customer.id,
                    Payment.paid_on >= args.from_date,
                    Payment.paid_on <= args.to_date,
                )
                .order_by(Payment.paid_on.desc(), Payment.id)
            )
        )
        if args.near_amount:
            target = _minor(args.near_amount, case.invoice.currency)
            payments.sort(key=lambda p: abs(p.amount - target))
        return [
            {
                "id": str(p.id),
                "amount": format_money(p.amount, p.currency),
                "paid_on": p.paid_on.isoformat(),
                "reference": p.reference,
            }
            for p in payments
        ]

    def get_contact_history(_: NoInput) -> list[dict[str, Any]]:
        messages = session.scalars(
            select(Message)
            .join(MessageCase, MessageCase.message_id == Message.id)
            .where(MessageCase.case_id == case.id, Message.tenant_id == tenant.id)
            .order_by(Message.created_at)
        )
        return [
            {
                "id": str(m.id),
                "direction": "to customer"
                if m.direction == Direction.OUTBOUND
                else "from customer",
                "date": f"{m.created_at:%Y-%m-%d}",
                "subject": m.subject,
                "text": m.body[:MAX_MESSAGE_CHARS],
            }
            for m in messages
        ]

    def get_promises(_: NoInput) -> list[dict[str, Any]]:
        rows = session.execute(
            select(Promise, Invoice.number)
            .join(Case, Case.id == Promise.case_id)
            .join(Invoice, Invoice.id == Case.invoice_id)
            .where(Promise.tenant_id == tenant.id, Case.customer_id == customer.id)
            .order_by(Promise.promised_date)
        )
        return [
            {
                "id": str(promise.id),
                "invoice": number,
                "amount": format_money(promise.amount, case.invoice.currency),
                "promised_date": promise.promised_date.isoformat(),
                "status": str(promise.status),
            }
            for promise, number in rows
        ]

    return [
        Tool(
            "get_invoice",
            "Current amount due and status of one of this customer's invoices.",
            GetInvoiceIn,
            get_invoice,
        ),
        Tool(
            "search_payments",
            "Payments recorded from this customer between two dates.",
            SearchPaymentsIn,
            search_payments,
        ),
        Tool(
            "get_contact_history",
            "Messages exchanged with the customer about this case.",
            NoInput,
            get_contact_history,
        ),
        Tool(
            "get_promises",
            "This customer's past promises to pay, and whether each was kept.",
            NoInput,
            get_promises,
        ),
    ]


def _minor(amount: str, currency: str) -> int:
    """Parse the model's amount for sorting; a bad amount is reported to the model."""
    try:
        return to_minor_units(amount, currency)
    except MoneyError as exc:
        raise ToolError(f"near_amount: {exc}") from None
