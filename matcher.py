from __future__ import annotations

import os
import uuid
from typing import Any

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from nekro_agent.api import core

from . import MEDIA_CACHE, MediaConfig, plugin
from .locator import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, extract_media_info
from .types import MediaCacheEntry, MediaStatus


media_interceptor = on_message(priority=10, block=False)


@media_interceptor.handle()
async def handle_media_message(bot: Bot, event: MessageEvent) -> None:
    config = plugin.get_config(MediaConfig)
    if not config.GEMINI_API_KEY:
        return
    _normalize_file_segments(event.message)
    target = next((segment for segment in event.message if segment.type in {"video", "record", "file"}), None)
    if target is None:
        return
    segments = [{"type": segment.type, "data": dict(segment.data)} for segment in event.message]
    media_info = await extract_media_info(bot, segments)
    if media_info is None:
        return
    sender = event.sender.card or event.sender.nickname or "群友"
    media_id = uuid.uuid4().hex[:8]
    context_text = "".join(segment.data.get("text", "") for segment in event.message if segment.type == "text")
    MEDIA_CACHE[media_id] = MediaCacheEntry(
        **media_info,
        sender_name=sender,
        context_text=context_text,
        message_id=event.message_id,
        bot_id=bot.self_id,
        status=MediaStatus.PENDING,
    )
    type_name = "视频" if media_info["type"] == "video" else "语音"
    notice = (
        f"【本地{type_name}挂载成功｜句柄 ID：{media_id}】\n"
        f"【调用方式】使用 analyze_media_file，media_id='{media_id}'。"
    )
    if context_text.strip():
        notice += f"\n【上下文】\n{context_text}"
    _replace_target(event, notice)
    core.logger.success(f"[Media] 媒体句柄已创建：{media_id}")


def _normalize_file_segments(message: Message) -> None:
    for segment in message:
        if segment.type != "file":
            continue
        if "size" in segment.data:
            try:
                segment.data["size"] = int(segment.data["size"])
            except (TypeError, ValueError):
                segment.data.pop("size", None)
        segment.data.setdefault("file_name", segment.data.get("name") or segment.data.get("file", ""))
        path = str(segment.data.get("file", ""))
        if path and not (path.startswith("/") or path.startswith("file://") or (len(path) > 2 and path[1] == ":")):
            segment.data.pop("file", None)


def _replace_target(event: MessageEvent, notice: str) -> None:
    replaced = False
    result = Message()
    for segment in event.message:
        if segment.type in {"video", "record"} and not replaced:
            result.append(MessageSegment.text(notice))
            replaced = True
            continue
        if segment.type == "file" and not replaced:
            extension = os.path.splitext(str(segment.data.get("file_name") or segment.data.get("file") or ""))[1].lower()
            if extension in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
                result.append(MessageSegment.text(notice))
                replaced = True
                continue
        result.append(segment)
    event.message.clear()
    event.message.extend(result)
