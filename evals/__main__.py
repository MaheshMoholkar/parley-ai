"""Run the evals, check the CI gate, or record a new baseline.

    python -m evals run replies --split test         # one eval
    python -m evals run all --split test              # all four
    python -m evals gate                              # compare reports/ with the baseline
    python -m evals baseline                          # make the current reports the baseline
    python -m evals compare-tiers --split test        # is the small model as good as the large?

Reports go to reports/<eval>.json and .md. The evals call the real model, so
they need PARLEY_MODEL_PROVIDER=bedrock and AWS access, and they cost money.
Tune prompts on --split dev; the test split is the held-out score.
"""

import argparse
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evalkit import Example, Rule, Scorer, check_gate, compare, run_eval, split
from evals import drafts, investigations, personas, replies
from evals.sandbox import prepare_database
from parley.adapters.models.bedrock import BedrockModel
from parley.adapters.models.bedrock_agent import BedrockAgentModel
from parley.config import Settings, get_settings
from parley.ports.model import ModelPort

REPORTS = Path("reports")
BASELINE = Path(__file__).parent / "baseline.json"
EVALS = ("replies", "drafts", "investigations", "personas")


@dataclass(frozen=True)
class EvalSpec:
    load: Callable[[], list[Example]]
    task: Callable[[], Callable[[Example], Any]]
    scorers: list[Scorer]
    rules: list[Rule]
    count_metrics: frozenset[str]


def specs(settings: Settings, routing: replies.Routing) -> dict[str, EvalSpec]:
    model = _model(settings)
    agent = BedrockAgentModel(settings.aws_region, settings.model_large, settings.effort_large)
    database: list[Any] = []  # created on first use, only for the evals that need it

    def db() -> Any:
        if not database:
            database.append(prepare_database())
        return database[0]

    return {
        "replies": EvalSpec(
            replies.load,
            lambda: replies.make_task(model, routing),
            replies.SCORERS,
            replies.RULES,
            replies.COUNT_METRICS,
        ),
        "drafts": EvalSpec(
            drafts.load,
            lambda: drafts.make_task(model),
            drafts.SCORERS,
            drafts.RULES,
            drafts.COUNT_METRICS,
        ),
        "investigations": EvalSpec(
            investigations.load,
            lambda: investigations.make_task(db(), lambda _: agent),
            investigations.SCORERS,
            investigations.RULES,
            investigations.COUNT_METRICS,
        ),
        "personas": EvalSpec(
            personas.load,
            lambda: personas.make_task(db(), model, lambda _: agent),
            personas.SCORERS,
            personas.RULES,
            personas.COUNT_METRICS,
        ),
    }


def _model(settings: Settings) -> ModelPort:
    if settings.model_provider != "bedrock":
        sys.exit(
            "The evals call the real model: set PARLEY_MODEL_PROVIDER=bedrock (and AWS access)."
        )
    return BedrockModel(
        region=settings.aws_region,
        model_ids={"large": settings.model_large, "small": settings.model_small},
        effort={"large": settings.effort_large, "small": settings.effort_small},
    )


def cmd_run(args: argparse.Namespace) -> int:
    names = EVALS if args.name == "all" else (args.name,)
    all_specs = specs(get_settings(), args.routing)
    REPORTS.mkdir(exist_ok=True)
    for name in names:
        spec = all_specs[name]
        examples = split(spec.load(), args.split)[: args.limit or None]
        print(f"running {name}: {len(examples)} examples ({args.split})", flush=True)
        report = run_eval(
            name, examples, spec.task(), spec.scorers, spec.count_metrics, args.workers
        )
        report.info = {"split": args.split, "routing": args.routing}
        (REPORTS / f"{name}.json").write_text(report.to_json(), encoding="utf-8")
        (REPORTS / f"{name}.md").write_text(report.to_markdown(), encoding="utf-8")
        print(report.to_markdown())
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    passed = True
    lines = ["# Eval gate", ""]
    for path in sorted(REPORTS.glob("*.json")):
        data = json.loads(path.read_text())
        name = data["name"]
        result = check_gate(data["metrics"], data["counts"], RULES[name], baseline.get(name))
        passed &= result.passed
        lines += [f"## {name} ({data['n']} examples, {data['info'].get('split', '?')})", "", "```"]
        lines += [*result.lines, "```", ""]
    if not baseline:
        lines.append(
            "No baseline yet: accuracy rules are not enforced. Run `python -m evals baseline`."
        )
    print("\n".join(lines))
    (REPORTS / "gate.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if passed else 1


TIER_METRICS = ["intent_correct", "date_correct", "amount_correct", "language_correct"]


def cmd_compare_tiers(args: argparse.Namespace) -> int:
    """Reply reading on the small model alone vs the large model alone (spec M5:
    the small tier must match the large tier within the gate)."""
    settings = get_settings()
    model = _model(settings)
    examples = split(replies.load(), args.split)
    folder = REPORTS / "tiers"
    folder.mkdir(parents=True, exist_ok=True)
    reports = {}
    for tier in ("small", "large"):
        print(f"running replies on the {tier} model: {len(examples)} examples", flush=True)
        report = run_eval(
            f"replies ({tier})",
            examples,
            replies.make_task(model, tier),
            replies.SCORERS,
            replies.COUNT_METRICS,
            args.workers,
        )
        (folder / f"replies_{tier}.json").write_text(report.to_json(), encoding="utf-8")
        print(report.to_markdown())
        reports[tier] = report
    result = compare(
        reports["small"].metrics(),
        reports["large"].metrics(),
        reports["large"].counts(),
        TIER_METRICS,
    )
    print("\n".join(["# Small vs large", *result.lines]))
    return 0 if result.passed else 1


def cmd_baseline(_: argparse.Namespace) -> int:
    baseline = {}
    for path in sorted(REPORTS.glob("*.json")):
        data = json.loads(path.read_text())
        if data["info"].get("split") != "test":
            sys.exit(
                f"{path} was run on the {data['info'].get('split')} split; baselines use test."
            )
        baseline[data["name"]] = data["metrics"]
    BASELINE.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
    print(f"wrote {BASELINE} for: {', '.join(baseline)}")
    return 0


RULES: dict[str, list[Rule]] = {
    "replies": replies.RULES,
    "drafts": drafts.RULES,
    "investigations": investigations.RULES,
    "personas": personas.RULES,
}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one eval, or all")
    run.add_argument("name", choices=(*EVALS, "all"))
    run.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    run.add_argument(
        "--routing",
        choices=("routed", "small", "large"),
        default="routed",
        help="reply reading: production routing, or one tier only",
    )
    run.add_argument("--limit", type=int, default=0, help="run only the first N examples")
    run.add_argument("--workers", type=int, default=4)
    run.set_defaults(func=cmd_run)

    commands.add_parser("gate", help="check reports/ against the baseline").set_defaults(
        func=cmd_gate
    )
    commands.add_parser("baseline", help="record reports/ as the baseline").set_defaults(
        func=cmd_baseline
    )
    tiers = commands.add_parser("compare-tiers", help="small vs large model on reply reading")
    tiers.add_argument("--split", choices=("dev", "test", "all"), default="test")
    tiers.add_argument("--workers", type=int, default=4)
    tiers.set_defaults(func=cmd_compare_tiers)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
