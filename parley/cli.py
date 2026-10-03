"""Command-line tool, installed as `parley`.

parley create-tenant --name "Acme Traders" --invoices-csv aging.csv
parley create-tenant --name "Acme Traders" --adapter-config acme.json --webhook-url https://...
parley set-webhook --tenant-id <id> --url https://...   (--url "" removes it)
parley sync --tenant-id <id>
parley worker            (runs until stopped)
parley worker --once     (one round, then exit)
"""

import argparse
import json
import uuid
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from parley.bootstrap import build_runtime, configure_logging
from parley.config import get_settings
from parley.services.sync import sync_tenant
from parley.services.tenants import create_tenant, set_webhook_url
from parley.services.worker import run_forever, run_once


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="parley", description="Collections agent tools")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-tenant", help="create a tenant")
    create.add_argument("--name", required=True)
    create.add_argument("--timezone", default="Asia/Kolkata")
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument("--invoices-csv", help="read invoices from this aging report CSV")
    source.add_argument(
        "--adapter-config", type=Path, help="JSON file with the accounting adapter's settings"
    )
    create.add_argument("--payments-csv", help="with --invoices-csv: a payments CSV")
    create.add_argument("--webhook-url", help="where to post outbound events")

    webhook = commands.add_parser("set-webhook", help="set or remove a tenant's webhook URL")
    webhook.add_argument("--tenant-id", required=True, type=uuid.UUID)
    webhook.add_argument("--url", required=True, help='an https URL, or "" to remove it')

    sync = commands.add_parser("sync", help="sync one tenant from its source now")
    sync.add_argument("--tenant-id", required=True, type=uuid.UUID)

    worker = commands.add_parser("worker", help="run the background worker")
    worker.add_argument("--once", action="store_true", help="run one round and exit")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    rt = build_runtime(settings)

    if args.command == "create-tenant":
        if args.adapter_config:
            adapter_config = json.loads(args.adapter_config.read_text(encoding="utf-8"))
        else:
            adapter_config = {"kind": "csv", "invoices_path": args.invoices_csv}
            if args.payments_csv:
                adapter_config["payments_path"] = args.payments_csv
        with rt.session_factory.begin() as session:
            tenant, api_key = create_tenant(
                session, args.name, args.timezone, adapter_config, webhook_url=args.webhook_url
            )
        print(f"tenant id:      {tenant.id}")
        print(f"API key:        {api_key}   (shown once; store it now)")
        print(f"webhook secret: {tenant.webhook_secret}   (signs events in and out)")

    elif args.command == "set-webhook":
        with rt.session_factory.begin() as session:
            set_webhook_url(session, args.tenant_id, args.url or None)
        print("webhook URL removed" if not args.url else f"webhook URL set to {args.url}")

    elif args.command == "sync":
        for name, value in asdict(sync_tenant(rt, args.tenant_id)).items():
            print(f"{name}: {value}")

    elif args.command == "worker":
        interval = timedelta(minutes=settings.sync_interval_minutes)
        if args.once:
            run_once(rt, interval)
        else:
            run_forever(rt, interval, settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
