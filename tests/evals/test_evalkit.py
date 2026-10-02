import json
from pathlib import Path

import pytest

from evalkit import Example, Rule, check_gate, load_jsonl, noise_margin, run_eval, split
from evalkit.dataset import DatasetError


def test_load_jsonl_skips_comments_and_checks_ids(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text(
        "// comment\n"
        + json.dumps({"id": "a", "input": {}, "expected": {}, "split": "test", "tags": ["x"]})
        + "\n"
        + json.dumps({"id": "b", "input": {}, "expected": {}})
        + "\n"
    )
    examples = load_jsonl(path)
    assert [e.id for e in examples] == ["a", "b"]
    assert [e.id for e in split(examples, "dev")] == ["b"]
    path.write_text(path.read_text() + json.dumps({"id": "a", "input": {}, "expected": {}}) + "\n")
    with pytest.raises(DatasetError, match="duplicate id"):
        load_jsonl(path)


def test_report_averages_rates_and_sums_counts() -> None:
    examples = [
        Example(str(i), {"v": i}, {}, tags=("even" if i % 2 == 0 else "odd",)) for i in range(4)
    ]

    def task(example: Example) -> int:
        if example.input["v"] == 3:
            raise ValueError("boom")
        return int(example.input["v"])

    report = run_eval(
        "toy",
        examples,
        task,
        [lambda ex, out: {"correct": out < 2, "bad": int(out == 2)}],
        count_metrics=frozenset({"bad"}),
    )
    metrics = report.metrics()
    assert metrics["correct"] == pytest.approx(2 / 4)  # the crash counts as wrong
    assert metrics["bad"] == 1
    assert metrics["errors"] == 1
    assert report.metrics("even")["correct"] == pytest.approx(1 / 2)
    assert "| correct | 50.0% |" in report.to_markdown()


def test_noise_margin_shrinks_with_more_examples() -> None:
    assert noise_margin(0.9, 50) > noise_margin(0.9, 500)
    assert noise_margin(1.0, 50) == pytest.approx(1 / 50)  # never less than one example


def test_gate_rules() -> None:
    rules = [Rule("acc", "no_drop"), Rule("wrong", "max", 0)]
    baseline = {"acc": 0.90}
    counts = {"acc": 60}
    assert check_gate({"acc": 0.88, "wrong": 0}, counts, rules, baseline).passed  # within noise
    assert not check_gate({"acc": 0.70, "wrong": 0}, counts, rules, baseline).passed
    assert not check_gate({"acc": 0.95, "wrong": 1}, counts, rules, baseline).passed
    no_baseline = check_gate({"acc": 0.10, "wrong": 0}, counts, rules, None)
    assert no_baseline.passed and "no baseline yet" in no_baseline.lines[0]
