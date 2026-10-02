"""The CI gate: compare an eval run with the baseline and the hard rules.

Two kinds of rule:
- "max": a hard limit that never moves, e.g. wrong-amount drafts must be 0.
- "no_drop": an accuracy that must not fall below the baseline by more than its
  noise margin. On a small test set one example moves a rate a lot (on 50
  examples, one example is 2 points), so a fixed margin would fail at random.
"""

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Rule:
    metric: str
    kind: Literal["max", "no_drop"]
    limit: float = 0.0  # for "max"


@dataclass(frozen=True)
class GateResult:
    passed: bool
    lines: list[str]


def noise_margin(rate: float, n: int) -> float:
    """How far a rate measured on n examples can move by chance: the 95% interval
    of a proportion, and never less than one example's worth."""
    if n <= 0:
        return 1.0
    return max(1.96 * math.sqrt(max(rate * (1 - rate), 0.0) / n), 1.0 / n)


def check_gate(
    metrics: dict[str, float],
    counts: dict[str, int],
    rules: list[Rule],
    baseline: dict[str, float] | None,
) -> GateResult:
    lines: list[str] = []
    passed = True
    for rule in rules:
        value = metrics.get(rule.metric)
        if value is None:
            lines.append(f"FAIL {rule.metric}: missing from the run")
            passed = False
            continue
        if rule.kind == "max":
            ok = value <= rule.limit
            lines.append(
                f"{'ok  ' if ok else 'FAIL'} {rule.metric} = {value:g} (limit {rule.limit:g})"
            )
            passed &= ok
            continue
        if baseline is None or rule.metric not in baseline:
            lines.append(f"note {rule.metric} = {value:.1%}: no baseline yet")
            continue
        base = baseline[rule.metric]
        margin = noise_margin(base, counts.get(rule.metric, 0))
        ok = value >= base - margin
        lines.append(
            f"{'ok  ' if ok else 'FAIL'} {rule.metric} = {value:.1%} "
            f"(baseline {base:.1%}, allowed drop {margin:.1%})"
        )
        passed &= ok
    return GateResult(passed, lines)
