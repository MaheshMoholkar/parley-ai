"""Eval examples, loaded from JSON Lines (one JSON object per line).

Each example has an id, an input for the task, the expected answer, a split
("dev" for tuning, "test" for the held-out score) and optional tags used to
break results down (for example by language).
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPLITS = ("dev", "test")


@dataclass(frozen=True)
class Example:
    id: str
    input: dict[str, Any]
    expected: dict[str, Any]
    split: str = "dev"
    tags: tuple[str, ...] = field(default_factory=tuple)


class DatasetError(ValueError):
    pass


def load_jsonl(path: Path | str) -> list[Example]:
    examples: list[Example] = []
    seen: set[str] = set()
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            raw = json.loads(line)
            example = Example(
                id=str(raw["id"]),
                input=raw["input"],
                expected=raw["expected"],
                split=raw.get("split", "dev"),
                tags=tuple(raw.get("tags", ())),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise DatasetError(f"{path}:{line_no}: {exc}") from None
        if example.split not in SPLITS:
            raise DatasetError(f"{path}:{line_no}: unknown split {example.split!r}")
        if example.id in seen:
            raise DatasetError(f"{path}:{line_no}: duplicate id {example.id!r}")
        seen.add(example.id)
        examples.append(example)
    return examples


def split(examples: Iterable[Example], name: str) -> list[Example]:
    """The examples of one split, or all of them for "all"."""
    return [e for e in examples if name == "all" or e.split == name]
