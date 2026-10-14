"""Idempotent NoneBot matcher registration used by plugin lifecycle hooks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def ensure_matcher(
    current: Any,
    on_message: Callable[..., Any],
    handler: Callable[..., Any],
    *,
    priority: int,
) -> Any:
    if current is not None:
        return current
    matcher = on_message(priority=priority, block=False)
    matcher.handle()(handler)
    return matcher


__all__ = ["ensure_matcher"]
