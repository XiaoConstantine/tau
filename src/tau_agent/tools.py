"""Pi-compatible provider-neutral tool definitions and execution results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field, model_validator

from tau_agent.messages import ImageContent, TextContent, ToolCall, WireModel
from tau_agent.types import JSONValue


class ToolCancellationToken(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether tool execution should stop."""
        ...


class AgentToolResult(WireModel):
    """Final or partial result produced by a tool."""

    content: list[TextContent | ImageContent] = Field(default_factory=list)
    details: JSONValue = None
    added_tool_names: list[str] | None = None
    terminate: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_text_content(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        return data

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))


class ToolCallRenderer(Protocol):
    def __call__(self, arguments: Mapping[str, JSONValue]) -> str | None:
        """Return a frontend-friendly tool invocation, or ``None``."""
        ...


class ToolResultRenderer(Protocol):
    def __call__(self, result: AgentToolResult, *, expanded: bool) -> str | None:
        """Return frontend markup for a tool result, or ``None``."""
        ...


ToolUpdateCallback = Callable[[AgentToolResult], None]


class ToolExecutor(Protocol):
    def __call__(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> Awaitable[AgentToolResult]:
        """Execute one validated tool call."""
        ...


ToolExecutionMode = Literal["sequential", "parallel"]
ToolEffect = Literal["pure", "read", "write", "execute", "network", "unknown"]
ToolEnforcement = Literal["host_confined", "host_asserted", "unknown"]
ToolArgumentPreparer = Callable[[object], Mapping[str, JSONValue]]


@dataclass(frozen=True, slots=True)
class AgentToolProvenance:
    """Host-supplied origin and enforcement metadata for one tool definition."""

    source: Literal["builtin", "extension", "host", "unknown"] = "unknown"
    identifier: str | None = None
    generation: str | None = None
    enforcement: ToolEnforcement = "unknown"

    def __post_init__(self) -> None:
        if self.source not in ("builtin", "extension", "host", "unknown"):
            raise ValueError(f"unsupported tool provenance source: {self.source!r}")
        if self.enforcement not in ("host_confined", "host_asserted", "unknown"):
            raise ValueError(f"unsupported tool enforcement: {self.enforcement!r}")
        for name, value in (("identifier", self.identifier), ("generation", self.generation)):
            if value is not None and (not value or len(value) > 512):
                raise ValueError(f"tool provenance {name} must contain 1 to 512 characters")


@dataclass(frozen=True, slots=True)
class AgentToolCallPreparation:
    """Canonical arguments or a host-owned result that blocks execution."""

    arguments: Mapping[str, JSONValue]
    blocked_result: AgentToolResult | None = None


class ToolCallPreparer(Protocol):
    def __call__(
        self,
        arguments: Mapping[str, JSONValue],
    ) -> Awaitable[AgentToolCallPreparation]:
        """Prepare one call before schema validation and execution."""
        ...


@dataclass(frozen=True, slots=True)
class AgentTool:
    """A tool exposed to the portable agent loop."""

    name: str
    label: str
    description: str
    parameters: Mapping[str, JSONValue]
    execute_fn: ToolExecutor
    prompt_snippet: str | None = None
    prompt_guidelines: tuple[str, ...] = ()
    prepare_arguments: ToolArgumentPreparer | None = None
    execution_mode: ToolExecutionMode = "parallel"
    render_call: ToolCallRenderer | None = None
    render_result: ToolResultRenderer | None = None
    prepare_call_fn: ToolCallPreparer | None = None
    effect: ToolEffect = "unknown"
    provenance: AgentToolProvenance = AgentToolProvenance()

    def __post_init__(self) -> None:
        if self.effect not in ("pure", "read", "write", "execute", "network", "unknown"):
            raise ValueError(f"unsupported tool effect: {self.effect!r}")

    @property
    def input_schema(self) -> Mapping[str, JSONValue]:
        """Alias used by provider payload builders."""
        return self.parameters

    async def prepare_call(
        self,
        arguments: Mapping[str, JSONValue],
    ) -> AgentToolCallPreparation:
        """Return canonical arguments or a result that blocks execution."""
        if self.prepare_call_fn is not None:
            return await self.prepare_call_fn(arguments)
        prepared = (
            self.prepare_arguments(arguments) if self.prepare_arguments is not None else arguments
        )
        return AgentToolCallPreparation(arguments=prepared)

    async def execute_prepared(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Execute arguments already canonicalized by :meth:`prepare_call`."""
        return await self.execute_fn(tool_call_id, arguments, signal, on_update)

    async def execute(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Prepare and execute a tool with Pi-compatible lifecycle semantics."""
        preparation = await self.prepare_call(arguments)
        if preparation.blocked_result is not None:
            return preparation.blocked_result
        return await self.execute_prepared(
            tool_call_id,
            preparation.arguments,
            signal,
            on_update,
        )


__all__ = [
    "AgentTool",
    "AgentToolProvenance",
    "AgentToolCallPreparation",
    "AgentToolResult",
    "ToolCall",
    "ToolCallPreparer",
    "ToolCallRenderer",
    "ToolCancellationToken",
    "ToolEffect",
    "ToolEnforcement",
    "ToolExecutionMode",
    "ToolResultRenderer",
    "ToolExecutor",
    "ToolUpdateCallback",
]
