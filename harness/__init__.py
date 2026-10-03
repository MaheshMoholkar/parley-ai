"""A small, generic agent harness: the loop, the tool registry, limits and a run log.

It knows nothing about invoices or collections; a project supplies the tools,
the prompt and the shape of the final answer. CI checks that nothing here
imports from `parley`.
"""

from harness.loop import Limits, RunResult, StepRecord, run_agent
from harness.model import (
    AgentModel,
    AgentModelError,
    AgentSession,
    FinalAnswer,
    Reply,
    TextOnly,
    ToolCall,
    ToolResult,
    Usage,
)
from harness.tools import Tool, ToolError

__all__ = [
    "AgentModel",
    "AgentModelError",
    "AgentSession",
    "FinalAnswer",
    "Limits",
    "Reply",
    "RunResult",
    "StepRecord",
    "TextOnly",
    "Tool",
    "ToolCall",
    "ToolError",
    "ToolResult",
    "Usage",
    "run_agent",
]
