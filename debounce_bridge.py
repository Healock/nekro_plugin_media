"""Optional bridge for publishing media readiness to the debounce plugin."""

from __future__ import annotations

import time
from typing import Any


DEBOUNCE_MODULE_NAME = "nekro_plugin_debounce"
MEDIA_HINT_CAPABILITY_VERSION = 1


def publish_media_hint(event: Any, media_handle_id: str, media_type: Any) -> bool:
    """Publish a one-shot hint after the media handle is stored.

    Media remains usable when debounce is absent or an older version is loaded.
    """

    try:
        from nekro_agent.services.plugin.collector import plugin_collector

        debounce_plugin = plugin_collector.get_plugin_by_module_name(DEBOUNCE_MODULE_NAME)
        bridge = getattr(debounce_plugin, "debounce_bridge", None)
        if getattr(bridge, "media_hint_version", None) != MEDIA_HINT_CAPABILITY_VERSION:
            return False
        publish = getattr(bridge, "publish_media_hint", None)
        if not callable(publish):
            return False
        adapter_key, chat_key = _event_channel(event)
        message_id = str(getattr(event, "message_id", "") or "")
        if not chat_key or not message_id or not media_handle_id:
            return False
        media_type_value = getattr(media_type, "value", media_type)
        return bool(
            publish(
                {
                    "version": MEDIA_HINT_CAPABILITY_VERSION,
                    "hint_id": f"media:{message_id}:{media_handle_id}",
                    "adapter_key": adapter_key,
                    "chat_key": chat_key,
                    "message_id": message_id,
                    "media_handle_id": str(media_handle_id),
                    "media_type": str(media_type_value or "unknown"),
                    "created_at": time.time(),
                    "expires_at": time.time() + 120.0,
                },
            )
        )
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
        return False


def _event_channel(event: Any) -> tuple[str, str]:
    adapter_key = "onebot_v11"
    group_id = str(getattr(event, "group_id", "") or "0")
    if group_id != "0":
        return adapter_key, f"{adapter_key}-group_{group_id}"
    sender = getattr(event, "sender", None)
    user_id = str(getattr(event, "user_id", "") or getattr(sender, "user_id", ""))
    return adapter_key, f"{adapter_key}-private_{user_id}"


__all__ = ["publish_media_hint"]
