"""Runs a task over examples (in parallel, since model calls mostly wait) and scores each."""

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from evalkit.dataset import Example
from evalkit.report import EvalReport, ExampleResult, Score

log = logging.getLogger(__name__)

# A scorer looks at one example and the task's output and returns named scores.
Scorer = Callable[[Example, Any], dict[str, Score]]


def run_eval(
    name: str,
    examples: Sequence[Example],
    task: Callable[[Example], Any],
    scorers: Sequence[Scorer],
    count_metrics: frozenset[str] = frozenset(),
    max_workers: int = 4,
) -> EvalReport:
    def one(example: Example) -> ExampleResult:
        try:
            output = task(example)
        except Exception as exc:
            log.warning("example %s failed: %s", example.id, exc)
            return ExampleResult(
                example.id, example.tags, None, {}, error=f"{type(exc).__name__}: {exc}"
            )
        scores: dict[str, Score] = {}
        for scorer in scorers:
            scores.update(scorer(example, output))
        return ExampleResult(example.id, example.tags, output, scores)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(one, examples))
    return EvalReport(name, results, count_metrics)
