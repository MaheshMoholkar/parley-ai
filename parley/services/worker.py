"""The background worker: one loop that keeps every tenant's cases moving.

Each round, for each tenant:
1. sync from the source system, if the last sync is older than the interval
   or the source has pushed an event since;
2. open cases for invoices that have become overdue;
3. read customer replies and act on them;
4. investigate paid claims and disputes;
5. act on due cases (reminders, escalations, timeouts);
6. draft queued reminders (model, checks, approval gate);
7. deliver pending messages from the outbox (emails are sent, calls dialled);
8. close calls the phone provider never reported back on;
9. post pending events to the tenant's webhook.

Several worker processes can run at once; the row locks in each step keep them
from doing the same work twice.
"""

import logging
import signal
import threading
import uuid
from datetime import datetime, timedelta
from types import FrameType

from sqlalchemy import select

from parley.db.models import Tenant
from parley.services.calls import finish_stale_calls
from parley.services.cases import open_overdue_cases
from parley.services.delivery import deliver_pending_messages
from parley.services.drafting import draft_queued_messages
from parley.services.due_cases import run_due_cases
from parley.services.events import latest_event_at
from parley.services.investigation import run_investigations
from parley.services.replies import read_received_replies
from parley.services.runtime import Runtime
from parley.services.sync import sync_tenant
from parley.services.webhooks import deliver_webhooks

log = logging.getLogger(__name__)


def run_once(rt: Runtime, sync_interval: timedelta) -> None:
    """One round over all tenants. A failure in one tenant is logged and does
    not stop the others."""
    with rt.session_factory.begin() as session:
        tenants = session.execute(select(Tenant.id, Tenant.last_synced_at)).all()

    for tenant_id, last_synced_at in tenants:
        try:
            run_tenant_once(rt, tenant_id, last_synced_at, sync_interval)
        except Exception:
            log.exception("worker round failed for tenant %s", tenant_id)


def run_tenant_once(
    rt: Runtime,
    tenant_id: uuid.UUID,
    last_synced_at: datetime | None,
    sync_interval: timedelta,
) -> None:
    now = rt.clock.now()
    with rt.session_factory() as session:
        event_at = latest_event_at(session, tenant_id)
    due = last_synced_at is None or now - last_synced_at >= sync_interval
    pushed = event_at is not None and last_synced_at is not None and event_at > last_synced_at
    if due or pushed:
        try:
            sync_tenant(rt, tenant_id)
        except Exception:
            # Keep working with the data from the last good sync.
            log.exception("sync failed for tenant %s", tenant_id)

    with rt.session_factory.begin() as session:
        open_overdue_cases(session, session.get_one(Tenant, tenant_id), now)
    read_received_replies(rt, tenant_id)
    run_investigations(rt, tenant_id)
    run_due_cases(rt, tenant_id)
    draft_queued_messages(rt, tenant_id)
    deliver_pending_messages(rt, tenant_id)
    finish_stale_calls(rt, tenant_id)
    deliver_webhooks(rt, tenant_id)


def run_forever(rt: Runtime, sync_interval: timedelta, poll_seconds: float) -> None:
    """Run rounds until the process receives SIGINT or SIGTERM. The current round
    is allowed to finish, so no transaction is cut off half-way."""
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %d, stopping after this round", signum)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    log.info("worker started")
    while not stop.is_set():
        try:
            run_once(rt, sync_interval)
        except Exception:
            log.exception("worker round failed")
        stop.wait(poll_seconds)
    log.info("worker stopped")
