"""Loads versioned prompt files from the prompts/ folder."""

from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    version: str  # e.g. "draft_reminder.v1", stored with every output
    text: str


@cache
def load_prompt(version: str) -> Prompt:
    return Prompt(version=version, text=(PROMPT_DIR / f"{version}.md").read_text(encoding="utf-8"))
