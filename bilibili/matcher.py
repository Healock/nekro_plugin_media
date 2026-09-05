from __future__ import annotations

import asyncio
import re
from pathlib import Path

from nekro_agent.api import core
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from .api import BILI_REGEX, fetch_bilibili_context, resolve_bvid
from .downloader import auto_clean_temp_dir, find_cached_file, schedule_download


def _segment_text(segment: MessageSegment) -> str:
    if segment.type == "text":
        return segment.data.get("text", "").replace("\\/", "/")
    if segment.type == "json":
        return str(segment.data.get("data", "")).replace("\\/", "/")
    return ""


def extract_target_url(message: Message) -> str | None:
    for segment in message:
        if segment.type in {"text", "json"}:
            match = re.search(BILI_REGEX, _segment_text(segment))
            if match:
                return match.group(0)
    return None


def _value(context: object, key: str, default: object) -> object:
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default)


def format_context(bvid: str, context: object, config: object) -> str:
    formatter = getattr(context, "format_comments", None)
    comments = formatter(
        max(0, int(getattr(config, "comment_count", 3))),
        max(0, int(getattr(config, "comment_char_limit", 100))),
    ) if callable(formatter) else str(_value(context, "comments", "暂无评论数据"))
    return (
        f"\n【B 站视频】{bvid}\n【标题】{_value(context, 'title', '未知标题')}\n"
        f"【来源】{_value(context, 'uploader', '未知 UP 主')}\n"
        f"【简介】{_value(context, 'desc', '暂无简介')}\n【热评】\n{comments}\n"
    )


async def process_message(event: MessageEvent, config: object, data_dir: Path) -> None:
    target_url = extract_target_url(event.message)
    if not target_url:
        return
    bvid = await resolve_bvid(target_url)
    if not bvid:
        return
    temp_dir = data_dir / "bilibili"
    temp_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(auto_clean_temp_dir, temp_dir)
    context = await fetch_bilibili_context(bvid, config)
    if int(_value(context, "duration", 0)) > int(getattr(config, "max_duration", 30)) * 60:
        core.logger.warning(f"[Bilibili] 视频超过时长限制：{bvid}")
        return
    cached = find_cached_file(temp_dir, bvid)
    media_path = cached or temp_dir / f"{bvid}.mp4"
    if cached is None:
        schedule_download(bvid, media_path, str(getattr(config, "download_quality", "480p")), data_dir)
    replacement = Message()
    replaced = False
    for segment in event.message:
        is_target = bool(target_url in _segment_text(segment))
        if is_target and not replaced:
            replacement.append(MessageSegment.text(format_context(bvid, context, config)))
            replacement.append(MessageSegment.video(file=str(media_path.absolute())))
            replaced = True
        else:
            replacement.append(segment)
    if replaced:
        event.message.clear()
        event.message.extend(replacement)


bili_interceptor = on_message(priority=9, block=False)


@bili_interceptor.handle()
async def handle_bili_penetration(bot: Bot, event: MessageEvent) -> None:
    del bot
    from .. import config, plugin

    await process_message(event, config, plugin.get_plugin_data_dir())
