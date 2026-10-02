"""Eval results: one record per example, averages per metric and per tag."""

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

Score = float | bool | int


@dataclass
class ExampleResult:
    id: str
    tags: tuple[str, ...]
    output: Any
    scores: dict[str, Score]
    error: str = ""


@dataclass
class EvalReport:
    name: str
    results: list[ExampleResult]
    # Metrics that are counts (summed), not rates (averaged), e.g. "wrong_amount_drafts".
    count_metrics: frozenset[str] = frozenset()
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.results)

    def metrics(self, tag: str | None = None) -> dict[str, float]:
        """Rates are the mean over the examples that report the metric; counts are sums.
        Examples that crashed count as 0 for every rate."""
        rows = [r for r in self.results if tag is None or tag in r.tags]
        values: dict[str, list[float]] = defaultdict(list)
        names = {name for r in self.results for name in r.scores}
        for r in rows:
            for name in names:
                if name in r.scores:
                    values[name].append(float(r.scores[name]))
                elif r.error and name not in self.count_metrics:
                    values[name].append(0.0)
        out: dict[str, float] = {}
        for name, vals in sorted(values.items()):
            out[name] = sum(vals) if name in self.count_metrics else sum(vals) / len(vals)
        out["errors"] = float(sum(1 for r in rows if r.error))
        return out

    def counts(self) -> dict[str, int]:
        """How many examples each rate is based on (for the gate's noise margin)."""
        counts: dict[str, int] = defaultdict(int)
        for r in self.results:
            for name in r.scores:
                counts[name] += 1
        return dict(counts)

    def to_json(self) -> str:
        tags = sorted({t for r in self.results for t in r.tags})
        return json.dumps(
            {
                "name": self.name,
                "n": self.n,
                "metrics": self.metrics(),
                "counts": self.counts(),
                "by_tag": {t: self.metrics(t) for t in tags},
                "info": self.info,
                "results": [asdict(r) for r in self.results],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    def to_markdown(self) -> str:
        lines = [f"## {self.name} ({self.n} examples)", "", "| Metric | Value |", "| --- | --- |"]
        for name, value in self.metrics().items():
            shown = (
                f"{value:g}" if name in self.count_metrics or name == "errors" else f"{value:.1%}"
            )
            lines.append(f"| {name} | {shown} |")
        tags = sorted({t for r in self.results for t in r.tags})
        if tags:
            lines += [
                "",
                "| Tag | n | " + " | ".join(k for k in self.metrics() if k != "errors") + " |",
            ]
            keys = [k for k in self.metrics() if k != "errors"]
            lines.append("| --- " * (len(keys) + 2) + "|")
            for t in tags:
                m = self.metrics(t)
                n = sum(1 for r in self.results if t in r.tags)
                cells = [
                    f"{m.get(k, 0):g}" if k in self.count_metrics else f"{m.get(k, 0):.0%}"
                    for k in keys
                ]
                lines.append(f"| {t} | {n} | " + " | ".join(cells) + " |")
        failed = [
            r
            for r in self.results
            if r.error
            or not all(bool(v) for k, v in r.scores.items() if k not in self.count_metrics)
        ]
        if failed:
            lines += [
                "",
                f"Failing examples ({len(failed)}): " + ", ".join(r.id for r in failed[:40]),
            ]
        return "\n".join(lines) + "\n"
