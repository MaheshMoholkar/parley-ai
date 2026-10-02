"""Command-line tool, installed as `parley`.

parley create-tenant --name "Acme Traders" --invoices-csv aging.csv
parley sync --tenant-id <id>
parley worker            (runs until stopped)
parley worker --once     (one round, then exit)
"""

import argparse
import uuid
from dataclasses import asdict
from datetime import timedelta

from parley.bootstrap import build_runtime, configure_logging
from parley.config import get_settings
from parley.services.sync import sync_tenant
from parley.services.tenants import create_tenant
from parley.services.worker import run_forever, run_once


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="parley", description="Collections agent tools")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-tenant", help="create a tenant that reads CSV files")
    create.add_argument("--name", required=True)
    create.add_argument("--timezone", default="Asia/Kolkata")
    create.add_argument("--invoices-csv", required=True)
    create.add_argument("--payments-csv")

    sync = commands.add_parser("sync", help="sync one tenant from its source now")
    sync.add_argument("--tenant-id", required=True, type=uuid.UUID)

    worker = commands.add_parser("worker", help="run the background worker")
    worker.add_argument("--once", action="store_true", help="run one round and exit")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    rt = build_runtime(settings)

    if args.command == "create-tenant":
        adapter_config = {"kind": "csv", "invoices_path": args.invoices_csv}
        if args.payments_csv:
            adapter_config["payments_path"] = args.payments_csv
        with rt.session_factory.begin() as session:
            tenant, api_key = create_tenant(session, args.name, args.timezone, adapter_config)
        print(f"tenant id: {tenant.id}")
        print(f"API key:   {api_key}   (shown once; store it now)")

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
