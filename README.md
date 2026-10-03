# parley-ai

A collections agent: it chases overdue invoices for a business and hands a human
only the cases that need judgement. The full design is in [docs/spec.md](docs/spec.md).

**Status: all seven milestones (M1 to M7).** The service syncs invoices from a CSV aging
report or an ERP, runs each overdue invoice through the case state machine, drafts
reminders with Claude (on Amazon Bedrock) and checks every draft in code, sends
email through Amazon SES, reads customer replies, and records promises and
disputes. When a customer says "we already paid" or disputes an invoice, an
investigator agent checks the records with read-only tools and hands a person a
finding backed by record ids. A person approves drafts and takes over unclear
cases on a small review screen. An eval suite measures the model's work and
gates changes in CI. `GET /v1/metrics` reports collection results and model cost per case.
The source system can push change events, and every step of a case is posted
to the tenant's webhook. From a reminder number the tenant chooses, the agent
phones the customer instead (Amazon Nova 2 Sonic through Twilio), in Hindi or
English, and a promise made on the call goes through the same checks as one
made by email.

By default it runs with no model and no email provider: reminders use a fixed
template and are recorded instead of sent. See "Turning on the model and email".

## Quick start

You need [uv](https://docs.astral.sh/uv/) (Python package manager) and PostgreSQL 16,
or Docker.

```bash
# 1. Start Postgres (or use your own and set PARLEY_DATABASE_URL, see .env.example)
docker compose up -d db

# 2. Install dependencies and create the tables
uv sync
uv run alembic upgrade head

# 3. Create a tenant that reads the sample aging report
uv run parley create-tenant --name "Acme Traders" --invoices-csv tests/fixtures/aging_20.csv
#    -> prints a tenant id, an API key (shown once) and a webhook secret

# 4. Run one worker round: sync, open cases, queue and "send" reminders
uv run parley worker --once

# 5. Look at the result through the API
uv run uvicorn parley.api.app:create_app --factory --reload
curl -H "Authorization: Bearer <API key>" "localhost:8000/v1/cases?state=awaiting_reply"
```

Interactive API docs are at `http://localhost:8000/docs`, and the review screen
is at `http://localhost:8000/review`, while the API runs. New tenants start with
`approval_mode: all`, so every reminder waits on the review screen until a person
approves it.

## Turning on the model and email

Set these environment variables (or put them in `.env`; see `.env.example`):

| Variable | Meaning |
| --- | --- |
| `PARLEY_MODEL_PROVIDER=bedrock` | Draft with Claude and read replies. AWS credentials come from the standard AWS chain. |
| `PARLEY_AWS_REGION` | Region for Bedrock and SES, e.g. `ap-south-1`. |
| `PARLEY_MODEL_LARGE`, `PARLEY_MODEL_SMALL` | Bedrock model ids for the two tiers. Defaults: `anthropic.claude-opus-5-5` and `anthropic.claude-sonnet-5-5`. |
| `PARLEY_CHANNEL=ses` | Send email through SES. |
| `PARLEY_EMAIL_FROM` | Verified SES sender address. |
| `PARLEY_REPLY_DOMAIN` | Domain that receives replies (`reply+<token>@<domain>`) through SES receiving. |
| `PARLEY_INBOUND_SECRET` | Shared secret for `POST /v1/inbound/email`. Inbound mail is refused while it is empty. |

**Receiving replies.** Configure an SES receipt rule for the reply domain that
stores each email in S3, and a small forwarder (for example a Lambda) that posts
the raw email to `POST /v1/inbound/email` with the header
`X-Parley-Signature: sha256=<HMAC-SHA256 of the body with the inbound secret>`.

**What the model may do.** It drafts text and reads replies, nothing else. Every
draft is checked in code for amounts, due dates, invoice numbers and banned
phrases, and judged for tone; a draft that fails twice is replaced by the fixed
template and sent for approval. A reply can only propose a promise, dispute or
payment claim, and code validates each field before the state machine acts.
Every model call is logged with its prompt version, tokens, estimated cost and
latency in the `model_calls` table.

**The investigator.** A paid claim or dispute queues a run in `agent_runs`. The
agent (the large model) may only call four read-only tools, all limited to that
one customer: `get_invoice`, `search_payments`, `get_contact_history` and
`get_promises`. It must finish with a finding and the ids of the records it
relied on. Code checks every id exists for that customer; a finding that cites
missing or wrong records is set aside as "unclear". Either way a person gets the
task: the investigator never closes a case or records a payment. Limits per run:
8 steps, 2 tool retries, 20 rows per tool result, 60 seconds. Every step is
stored in the run, so a run can be replayed and scored.

## Connecting a source system and a webhook

Each accounting adapter is a folder in `parley/adapters/accounting/` with a
`KIND` and a `from_config` function; its docstring lists the settings it takes.
To create a tenant for an ERP instead of a CSV, put those settings in a JSON file:

```bash
uv run parley create-tenant --name "Acme Traders" --adapter-config acme.json \
    --webhook-url https://your-app.example/parley-events
uv run parley set-webhook --tenant-id <id> --url ""     # stop webhooks later
```

Passwords and tokens in adapter settings are references, never the secret
itself: `"env:NAME"` reads an environment variable, and `"aws:<secret arn>"`
reads AWS Secrets Manager.

**Events in.** The source system may post `invoice.created`, `invoice.updated`,
`invoice.voided` or `payment.recorded` to `POST /v1/events`:

```text
POST /v1/events
Authorization: Bearer <API key>
X-Parley-Signature: sha256=<HMAC-SHA256 of the body with the webhook secret>

{"id": "evt-123", "type": "payment.recorded", "data": {...}}
```

An event only asks for a fresh sync: the worker syncs that tenant on its next
round instead of waiting for the interval. Invoice facts still come only from
the sync, so a lost or repeated event does no harm (a repeated id is ignored).

**Webhooks out.** When the tenant has a webhook URL, these are posted to it:
`case.opened`, `message.sent`, `reply.received`, `promise.created`,
`promise.broken`, `dispute.opened`, `task.created`, `case.closed`.

```text
X-Parley-Event-Id: <uuid>          the same on every retry: ignore ids you have seen
X-Parley-Event-Type: case.opened
X-Parley-Signature: sha256=<HMAC-SHA256 of the body with the webhook secret>

{"id": "...", "type": "case.opened", "created_at": "...", "data": {"case_id": "...", ...}}
```

Events are written in the same transaction as the change (an outbox), then
posted oldest first. Any 2xx answer counts as delivered; otherwise the event
is retried after 1, 2, 4 ... minutes (at most 6 hours apart) and given up after
10 attempts. Delivery is at least once, and a retried event can arrive after a
newer one.

## Phone calls

Calls are off until you turn them on. Two parts:

| Variable | Meaning |
| --- | --- |
| `PARLEY_SPEECH_PROVIDER=nova_sonic` | The agent can talk: Amazon Nova 2 Sonic on Bedrock (`PARLEY_AWS_REGION`). Needed for both kinds of call below. |
| `PARLEY_VOICE_IDS` | Nova Sonic voice per language, as JSON, e.g. `{"en": "kiara", "hi": "kiara"}`. Check the ids against the Nova 2 Sonic voice list. |
| `PARLEY_VOICE=twilio` | Place reminder calls through Twilio. |
| `PARLEY_PUBLIC_URL` | This service's public `https://` address; Twilio calls it back. |
| `PARLEY_TWILIO_ACCOUNT_SID`, `PARLEY_TWILIO_AUTH_TOKEN`, `PARLEY_TWILIO_FROM_NUMBER` | The Twilio account and the number calls come from. |
| `PARLEY_VOICE_ALLOWED_NUMBERS` | Comma-separated numbers that may be called, or `*` for any. Empty means no one: demo calls go only to people who agreed. |

**Try it in the browser first.** With only the speech provider set, open
`http://localhost:8000/voice`, enter the API key and a case id, and talk to the
agent as if you were that customer. The call acts on the real case: a promise
you make is recorded.

**Reminder calls.** Choose the reminder from which customers are called, and
optionally a person's number for transfers:

```bash
uv run parley set-policy --tenant-id <id> '{"call_from_reminder": 3}'
uv run parley set-transfer-number --tenant-id <id> --number "+91 98xxxxxxxx"
```

From then on, a reminder at or after that number is a call when the customer
has a phone number that may be called (and always, if they have no email). Like
an email it waits for approval if the approval mode asks for it, and it is
dialled only outside quiet hours.

**What happens on a call.**

1. Twilio rings the customer. An answering machine gets no message: the call
   counts as an attempt and the next one is due the next day.
2. The agent says it is an AI assistant calling for the business, and asks to
   speak to the customer. It cannot see any amount until it calls
   `confirm_identity`, then `get_invoice`.
3. It can use `log_promise`, `log_dispute`, `send_payment_link`,
   `transfer_to_human` and `end_call`, and nothing else. A promise or dispute
   goes through the same code checks and state machine as an email reply, and
   the agent is told whether it was recorded. When the customer talks over
   the agent, audio not yet played is dropped.
4. Afterwards the transcript and every tool call are saved (the `calls` table,
   and as text in the message body), and audited in code: the AI disclosure in
   the first turn, no amount before the identity check, no amount that is not
   owed, no banned phrase. A call that fails the audit becomes a `review_call`
   task for a person.

Calls are limited to 7 minutes. Nova Sonic speaks Hindi and English but not
Marathi, so Marathi speakers are called in Hindi.

To run everything in containers, use `docker compose up --build`. Put CSV files in
`./data`, and pass `/data/<file>.csv` as the path when you create the tenant.

## Checks

These run in CI on every pull request ([.github/workflows/ci.yml](.github/workflows/ci.yml)):

```bash
uv run ruff format --check .      # formatting
uv run ruff check .               # lint
uv run mypy                       # type check (strict)
uv run lint-imports               # architecture rules (see pyproject.toml)
scripts/check_boundary.sh         # ERP-specific names stay inside their adapter
uv run alembic check              # migrations match the models
uv run pytest                     # unit + database tests (needs Postgres)
```

The database tests use `PARLEY_TEST_DATABASE_URL`, which defaults to a
`parley_test` database on localhost. Its contents are deleted on every run.

## Metrics and cost

`GET /v1/metrics` (optionally `?since=<time>`) returns cases by state, promises
made, kept and broken, average days from due date to collection, and model
spend: total, per case that used the model, by tier (small, large,
investigator) with tokens and latency, and the share of input served from the
prompt cache.

**Prompt caching.** System prompts and the investigator's tool list are marked
for caching, but Claude only caches a prefix of at least 512 tokens on the
current models. Today only the investigator's prefix (prompt plus tool
definitions) is that long; the single-call prompts are 200 to 470 tokens, so
they are not cached yet. Adding worked examples to the reply-reading prompt
would both lengthen it past the minimum and likely help accuracy, but it is a
prompt change, so make it as a new prompt version and measure it with the evals.
`cache_read_share` in the metrics shows the effect.

## Evals

The evals measure the parts that depend on the model, on real model calls:

| Eval | What it checks | Dataset |
| --- | --- | --- |
| `replies` | Intent, promised date and amount, language of a customer reply | 155 hand-labelled replies, 63 in Hindi, Marathi or Hinglish, 10 injection attempts |
| `drafts` | Reminders pass the code checks and the tone judge | 30 scenarios: 4 languages, 3 tones, adversarial briefs |
| `investigations` | The investigator's finding and the payments it cites | 26 seeded ledgers with known answers |
| `personas` | Whole cases over 30 simulated days; no policy violation in the log | 6 debtor personas played by the model |

```bash
PARLEY_MODEL_PROVIDER=bedrock uv run python -m evals run replies --split dev   # tune on dev
PARLEY_MODEL_PROVIDER=bedrock uv run python -m evals run all --split test      # held-out score
uv run python -m evals gate        # compare reports/ with evals/baseline.json
uv run python -m evals baseline    # accept the current test-split reports as the baseline
PARLEY_MODEL_PROVIDER=bedrock uv run python -m evals compare-tiers   # small vs large on replies
```

`compare-tiers` reads every test reply with the small model alone and with the
large model alone, and fails if the small one is worse by more than the noise
margin; that is the check behind using the small model first in production.

The gate has hard rules (no wrong-amount or banned-phrase drafts, no injection
that changes a reading, no policy violations) and accuracy rules: a metric may
not drop below the baseline by more than its noise margin, which depends on how
many examples it is measured on. Until a baseline is recorded, only the hard
rules apply. The `Evals` GitHub workflow runs them on pull requests that touch
prompts or model code and nightly, once the `AWS_EVAL_ROLE_ARN` repository
variable is set.

The labels in `evals/datasets/` were written for this project and should be
reviewed by someone who knows the customers and languages before the baseline
is trusted. The eval plumbing itself is tested with fake models in
`tests/integration/test_eval_plumbing.py`.

## Layout

```text
harness/         generic agent loop, tool registry, limits, run log; knows
                 nothing about invoices (CI enforces this)
evalkit/         generic eval runner, reports and CI gate (also project-free)
evals/           this project's eval datasets, tasks, scorers and CLI
parley/
  core/          pure rules: no database, no network. Start reading here.
    domain.py      names: case states, task kinds, statuses
    workflow.py    the case state machine (one function per event)
    policy.py      tenant settings, quiet hours, weekly contact cap, approval
    checks.py      the checks every draft must pass before it is sent
    money.py       amounts as whole paise
    messages.py    the fixed reminder template (fallback when drafts fail)
    voice.py       the audit every call transcript gets afterwards
    phone.py       phone numbers in the form telephony providers need
  collections_ai/ the model jobs and their versioned prompts (prompts/*.md)
  ports/         interfaces the core needs from the outside world
    accounting.py  read invoices, customers and payments from a source system
    channel.py     send a message; the shape of an inbound reply
    clock.py       tell the time (faked in tests)
    model.py       call a model and get typed output back
    voice.py       place calls, talk to a speech model, the caller's audio
  adapters/      implementations of the ports
    accounting/    one folder per source system: csv/ reads an aging report,
                   the others call an ERP's API (found by scanning the folder)
    secrets.py     resolves "env:" and "aws:" secret references
    webhook_http.py  posts one outbound webhook
    channels/      dry run, SES sending, inbound email parsing
      voice/       Twilio calls and media streams, the browser test leg, audio formats
    models/        Claude on Bedrock (single calls and agent sessions), Nova 2
                   Sonic for calls, a fake
    clock.py       real clock and fake clock
  db/            tables (models.py), connections, migrations
  services/      use cases that load data, call the core and save results
    sync.py        copy invoices from the source; close or reopen cases
    due_cases.py   act on due cases: remind, escalate, reschedule
    drafting.py    draft queued reminders, run checks, apply the approval gate
    inbound.py     receive and match a customer's email reply
    replies.py     read replies and act on them; update the customer brief
    investigation.py  the investigator's tools, runs, and the check on findings
    audit.py       finds policy violations in the sent-message log
    tasks.py       a person approving, editing, rejecting, resuming, closing
    delivery.py    send pending messages from the outbox
    events.py      record events pushed by the source system
    calls.py       phone calls: the agent's tools, finishing and auditing a call
    voice_bridge.py  carries a live call between the phone and the speech model
    webhooks.py    the outbound events outbox and its delivery
    worker.py      the background loop that runs all of the above
  api/           HTTP endpoints (FastAPI) and the review screen (review.html)
  cli.py         the `parley` command
tests/
  unit/          core rules and the CSV adapter (no database)
  integration/   services and API against a real Postgres
    test_simulation_30_days.py   the M1 acceptance test
    test_walkthrough.py          the M2 acceptance test
    test_investigation.py        the M3 acceptance tests
    test_events.py               events in and webhooks out
    test_voice.py                calls end to end (the M7 acceptance test)
  harness/       the agent loop on its own, with toy tools
  adapters/      each ERP adapter against a fake of that ERP's API, including
                 the walkthrough with the ERP as the source (the M6 acceptance test)
```

## Reading guide

A good order for reading the code:

1. `parley/core/workflow.py`: the whole case lifecycle as plain functions. Each one
   takes the current case and an event and returns a `Transition`.
2. `tests/unit/test_workflow.py`: one test per row of the spec's transition table.
3. `parley/services/due_cases.py`: how the worker uses those rules with the
   database.
4. `tests/integration/test_simulation_30_days.py`: 20 invoices over 30 simulated
   days, with the expected outcome for each customer written out at the top.

### Design choices worth knowing

- **Code decides, never the model.** The state machine in `core/workflow.py` picks
  every move. Amounts and dates in messages always come from the database.
- **One message per customer.** When several of a customer's invoices are due at
  the same time, they go out as one message. The weekly cap counts messages, not
  invoices.
- **Outbox for sends.** A reminder is written as a `pending` message in the same
  transaction as the case change, then a separate step sends it. A crash can never
  produce a case that thinks it sent a message it did not, and a retry reuses the
  same idempotency key.
- **Row locks for concurrency.** The worker locks one customer at a time with
  `FOR UPDATE SKIP LOCKED`, so several workers can run without contacting the
  same customer twice. Sync locks the tenant row, so two syncs never overlap.
- **Pause is a flag, not a state.** A paused case keeps its place in the workflow
  and picks up where it left off when resumed.
- **The source system owns invoice facts.** The core never edits an amount or
  marks an invoice paid. It reads them on every sync and closes cases when the
  source says the invoice is paid, void or gone.

### Python in this codebase, briefly

- `@dataclass(frozen=True)` is a small read-only record class.
- `class X(Protocol)` is an interface. Any class with matching methods counts;
  there's no need to inherit from it.
- `StrEnum` is a set of named constants that behave as strings (`"scheduled"`).
- `match ... case` is like a `switch` statement that can also match on types.
- `with session_factory.begin() as session:` opens a database transaction. It
  commits at the end of the block, or rolls back if an error escapes.
- `Mapped[int]` in `db/models.py` declares a table column and its Python type.
- Type hints (`-> int`, `str | None`) are checked by mypy in CI. They aren't
  enforced at runtime.
