from __future__ import annotations

import sys
import types

from nekro_plugin_media.debounce_bridge import _event_channel, publish_media_hint


class Event:
    message_id = 123
    group_id = 456
    user_id = 789


def test_event_channel_matches_nekro_onebot_keys() -> None:
    assert _event_channel(Event()) == ("onebot_v11", "onebot_v11-group_456")

    private = Event()
    private.group_id = 0
    assert _event_channel(private) == ("onebot_v11", "onebot_v11-private_789")


def test_publish_media_hint_uses_loaded_debounce_capability(monkeypatch) -> None:
    captured: list[dict] = []

    class Bridge:
        media_hint_version = 1

        def publish_media_hint(self, payload):
            captured.append(payload)
            return True

    collector = types.SimpleNamespace(
        get_plugin_by_module_name=lambda _name: types.SimpleNamespace(debounce_bridge=Bridge()),
    )
    module = types.ModuleType("nekro_agent.services.plugin.collector")
    module.plugin_collector = collector
    monkeypatch.setitem(sys.modules, "nekro_agent.services.plugin.collector", module)

    assert publish_media_hint(Event(), "handle-1", "video")
    assert captured[0]["message_id"] == "123"
    assert captured[0]["chat_key"] == "onebot_v11-group_456"
    assert captured[0]["media_handle_id"] == "handle-1"
    assert captured[0]["version"] == 1
    assert captured[0]["media_type"] == "video"


def test_missing_or_incompatible_debounce_capability_fails_open(monkeypatch) -> None:
    collector = types.SimpleNamespace(
        get_plugin_by_module_name=lambda _name: types.SimpleNamespace(
            debounce_bridge=types.SimpleNamespace(media_hint_version=2),
        ),
    )
    module = types.ModuleType("nekro_agent.services.plugin.collector")
    module.plugin_collector = collector
    monkeypatch.setitem(sys.modules, "nekro_agent.services.plugin.collector", module)

    assert not publish_media_hint(Event(), "handle-1", "video")
