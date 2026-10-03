"""Tools: a name, a description, a Pydantic input type and a handler.

Handlers return any JSON-serialisable value. Raising ToolError reports a
problem to the model (for example "no invoice with that number") without
counting as a crash.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class ToolError(Exception):
    """An expected failure, shown to the model as the tool result."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_type: type[BaseModel]
    handler: Callable[[Any], Any]  # called with an instance of input_type

    def input_schema(self) -> dict[str, Any]:
        return self.input_type.model_json_schema()
