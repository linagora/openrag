"""The agent an ``agentic`` request runs: its profile, picked by ``metadata.agent_profile``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.utils.exceptions import ValidationError

if TYPE_CHECKING:
    from core.config.agentic import AgenticConfig, AgentProfileConfig


@dataclass(frozen=True)
class AgentProfile:
    """One agent: its prompt, its tools, how it samples, how many rounds of tool calls it may make."""

    name: str
    #: A prompt of type ``agent`` in the library; ``None`` for the type's default.
    prompt_name: str | None
    tools: tuple[str, ...]
    #: Used when the request sends no temperature.
    temperature: float | None
    max_tool_rounds: int

    @classmethod
    def from_config(cls, name: str, config: AgentProfileConfig) -> AgentProfile:
        return cls(
            name=name,
            prompt_name=config.prompt_name or None,
            tools=tuple(config.tools),
            temperature=config.temperature,
            max_tool_rounds=config.max_tool_rounds,
        )


class AgentProfiles:
    """The configured agents, by name."""

    def __init__(self, config: AgenticConfig) -> None:
        self._profiles = {name: AgentProfile.from_config(name, profile) for name, profile in config.profiles.items()}
        self._default = config.default_profile

    def get(self, name: str | None) -> AgentProfile:
        """The profile named *name*, the default for ``None``.

        An unknown name is the client's error (400), never a silent fallback to
        another agent.
        """
        profile = self._profiles.get(self._default if name is None else name)
        if profile is None:
            raise ValidationError(
                f"Unknown agent_profile '{name}'. Profiles: {', '.join(sorted(self._profiles))}.",
                status_code=400,
                code="UNKNOWN_AGENT_PROFILE",
            )
        return profile

    def __iter__(self):
        return iter(self._profiles.values())


__all__ = ["AgentProfile", "AgentProfiles"]
