"""The ``agentic`` mode's agents: each is a profile, picked per request by ``metadata.agent_profile``.

An agent is data, not code: its prompt (a prompt of type ``agent`` in the
library, so admins edit it like any other), the tools it is offered, its
temperature and how many rounds of tool calls it may make before it must
answer.
"""

from __future__ import annotations

from core.llm.reasoning import ReasoningEffort
from pydantic import Field, model_validator

from .base import ConfigMixin

#: Every read-only tool, in the order the agent is shown them.
DEFAULT_AGENT_TOOLS = (
    "search_knowledge",
    "search_file",
    "read_file",
    "summarize",
    "find_files",
    "get_file_info",
)


class AgentProfileConfig(ConfigMixin):
    """One agent."""

    #: A prompt of type ``agent`` named in the library; empty for the type's default.
    prompt_name: str | None = None
    #: The tools it is offered, by name; an unknown name fails startup.
    tools: list[str] = Field(default_factory=lambda: list(DEFAULT_AGENT_TOOLS), min_length=1)
    #: Used when the request sends no temperature; ``None`` then leaves the endpoint's own.
    temperature: float | None = Field(default=0.2, ge=0, le=2)
    #: OpenAI's reasoning_effort, used when the request sends none; ``None`` then leaves the endpoint's.
    reasoning_effort: ReasoningEffort | None = None
    #: Rounds of tool calls before the agent must answer from what it found.
    max_tool_rounds: int = Field(default=6, ge=1, le=20)
    #: The best-ranked hits of each search shown in full, the others as excerpts:
    #: a model that answers without reading still answers from those in full.
    full_passages: int = Field(default=4, ge=0, le=20)


class AgenticConfig(ConfigMixin):
    """The agents a request can pick, and the one it gets when it names none."""

    default_profile: str = "default"
    profiles: dict[str, AgentProfileConfig] = Field(default_factory=lambda: {"default": AgentProfileConfig()})

    @model_validator(mode="after")
    def _default_profile_exists(self) -> AgenticConfig:
        if self.default_profile not in self.profiles:
            names = ", ".join(sorted(self.profiles)) or "none"
            raise ValueError(f"agentic.default_profile '{self.default_profile}' is not a profile (profiles: {names})")
        return self


__all__ = ["DEFAULT_AGENT_TOOLS", "AgentProfileConfig", "AgenticConfig"]
