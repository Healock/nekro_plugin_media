from __future__ import annotations


def destroy_matcher(matcher: object) -> None:
    destroy = getattr(matcher, "destroy", None)
    if not callable(destroy):
        return
    try:
        destroy()
    except (KeyError, ValueError):
        return
