from __future__ import annotations

from nekro_plugin_media.matcher_registration import ensure_matcher


class Matcher:
    def __init__(self) -> None:
        self.handlers = []

    def handle(self):
        return self.handlers.append


def test_ensure_matcher_reuses_registered_instance() -> None:
    current = Matcher()
    created = []

    def on_message(**_kwargs):
        created.append(True)
        return Matcher()

    result = ensure_matcher(current, on_message, lambda: None, priority=10)

    assert result is current
    assert created == []


def test_ensure_matcher_recreates_destroyed_instance_once() -> None:
    created = []

    def on_message(**kwargs):
        matcher = Matcher()
        created.append((kwargs, matcher))
        return matcher

    handler = lambda: None
    first = ensure_matcher(None, on_message, handler, priority=9)
    second = ensure_matcher(first, on_message, handler, priority=9)

    assert second is first
    assert len(created) == 1
    assert created[0][0] == {"priority": 9, "block": False}
    assert created[0][1].handlers == [handler]
