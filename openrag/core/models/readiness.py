"""Readiness discovery and public result models."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

from core.config.model_endpoints import ModelEndpointConfig, ModelEndpointType

ReadinessStatus = Literal["ok", "timeout", "unavailable", "unresolvable"]


class _ImmutableMapping(Mapping[str, ReadinessStatus]):
    __slots__ = ("_items",)

    def __init__(self, source: Mapping[str, ReadinessStatus]) -> None:
        object.__setattr__(self, "_items", tuple(source.items()))

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("Readiness checks are immutable")

    def __getitem__(self, key: str) -> ReadinessStatus:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __deepcopy__(self, memo: object) -> _ImmutableMapping:
        return self


@dataclass(frozen=True, slots=True)
class ModelEndpointTarget:
    provider: str
    kind: ModelEndpointType
    config: ModelEndpointConfig | None
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class ConfigurationReferenceFinding:
    kind: Literal["indexation_preset", "retrieval_preset"]
    name: str


@dataclass(frozen=True, slots=True)
class ModelEndpointDiscovery:
    targets: tuple[ModelEndpointTarget, ...] = ()
    configuration_references: tuple[ConfigurationReferenceFinding, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelEndpointReadiness:
    provider: str
    kind: ModelEndpointType
    status: ReadinessStatus


@dataclass(frozen=True, slots=True)
class ConfigurationReferenceReadiness:
    kind: Literal["indexation_preset", "retrieval_preset"]
    count: int
    status: Literal["unresolvable"] = "unresolvable"


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    checks: Mapping[str, ReadinessStatus]
    model_endpoints: tuple[ModelEndpointReadiness, ...] = ()
    configuration_references: tuple[ConfigurationReferenceReadiness, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", _ImmutableMapping(self.checks))
