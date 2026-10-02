"""A small, generic eval toolkit: datasets, a runner, reports and a CI gate.

It knows nothing about collections; a project supplies the examples, the task
under test and the scorers. CI checks that nothing here imports from `parley`.
"""

from evalkit.dataset import Example, load_jsonl, split
from evalkit.gate import GateResult, Rule, check_gate, noise_margin
from evalkit.report import EvalReport, ExampleResult
from evalkit.runner import Scorer, run_eval

__all__ = [
    "EvalReport",
    "Example",
    "ExampleResult",
    "GateResult",
    "Rule",
    "Scorer",
    "check_gate",
    "load_jsonl",
    "noise_margin",
    "run_eval",
    "split",
]
