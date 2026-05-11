"""Runner registry. Adapters convert a Case's inputs into an LLM call."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Runner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_REGISTRY: dict[str, Runner] = {}


def register(name: str, runner: Runner) -> None:
    if name in _REGISTRY:
        raise ValueError(f"runner already registered: {name}")
    _REGISTRY[name] = runner


def get(name: str) -> Runner:
    if name not in _REGISTRY:
        raise KeyError(
            f"no runner registered for {name!r}; known: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]


def known() -> list[str]:
    return sorted(_REGISTRY)


# Importing the modules below has the side effect of registering each runner.
from . import next_intent as _next_intent  # noqa: F401, E402
from . import unified_preprocess as _unified_preprocess  # noqa: F401, E402
