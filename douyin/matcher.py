"""抖音消息拦截：识别分享链接并把消息重写为可分析的媒体段落。

与主流程的耦合方式与 Bilibili 子模块完全一致——仅重写消息内容：
- 视频帖：追加 `MessageSegment.video(file=<本地待下载路径>)`，由主流程媒体拦截器接管分析；
- 图文帖：直接追加图片段落，不触发媒体分析流程。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from nekro_agent.api import core
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from .api import DOUYIN_URL_REGEX, fetch_douyin_context, resolve_aweme_id
from .downloader import auto_clean_temp_dir, find_cached_file, schedule_download

__all__ = [
    "douyin_interceptor",
    "extract_target_url",
    "format_context",
    "process_message",
]


def _segment_text(segment: MessageSegment) -> str:
    if segment.type == "text":
        return segment.data.get("text", "").replace("\\/", "/")
    if segment.type == "json":
        return str(segment.data.get("data", "")).replace("\\/", "/")
    return ""


def extract_target_url(message: Message) -> str | None:
    for segment in message:
        if segment.type in {"text", "json"}:
            match = re.search(DOUYIN_URL_REGEX, _segment_text(segment))
            if match:
                return match.group(0)
    return None


def _config_int(config: object, name: str, default: int) -> int:
    try:
        return max(0, int(getattr(config, name, default)))
    except (TypeError, ValueError):
        return default


def format_context(context: object, config: object, aweme_id: str) -> str:
    limit = _config_int(config, "douyin_desc_char_limit", 150)
    kind = "图集" if context.is_gallery else "视频"
    lines = [
        f"\n【抖音{kind}】{aweme_id}",
        f"【文案】{context.title(limit)}",
        f"【作者】{context.author}",
    ]
    if not context.is_gallery and context.duration:
        lines.append(f"【时长】{context.duration} 秒")
    if context.digg_count or context.comment_count:
        lines.append(f"【数据】{context.format_stats()}")
    if context.is_gallery:
        lines.append(f"【图片】{len(context.image_urls)} 张")
    return "\n".join(lines) + "\n"


async def process_message(event: MessageEvent, config: object, data_dir: Path) -> None:
    target_url = extract_target_url(event.message)
    if not target_url:
        return
    aweme_id = await resolve_aweme_id(target_url)
    if not aweme_id:
        return
    context = await fetch_douyin_context(aweme_id)
    if context is None:
        return

    max_minutes = _config_int(config, "douyin_max_duration", 30)
    if not context.is_gallery and max_minutes > 0 and context.duration > max_minutes * 60:
        core.logger.warning(f"[Douyin] 视频超过时长限制：{aweme_id}")
        return

    temp_dir = data_dir / "douyin"
    temp_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(auto_clean_temp_dir, temp_dir)

    notice = format_context(context, config, aweme_id)
    replacement = Message()
    replaced = False
    for segment in event.message:
        is_target = bool(target_url in _segment_text(segment))
        if is_target and not replaced:
            replaced = True
            replacement.append(MessageSegment.text(notice))
            if context.is_gallery:
                for image_url in context.image_urls:
                    replacement.append(MessageSegment.image(image_url))
            else:
                cached = find_cached_file(temp_dir, aweme_id)
                media_path = cached or temp_dir / f"{aweme_id}.mp4"
                if cached is None:
                    schedule_download(
                        aweme_id,
                        media_path,
                        context.video_url,
                        str(getattr(config, "douyin_download_quality", "720p")),
                        data_dir,
                    )
                replacement.append(MessageSegment.video(file=str(media_path.absolute())))
        else:
            replacement.append(segment)
    if replaced:
        event.message.clear()
        event.message.extend(replacement)


douyin_interceptor = on_message(priority=9, block=False)


@douyin_interceptor.handle()
async def handle_douyin_penetration(bot: Bot, event: MessageEvent) -> None:
    del bot
    from .. import config, plugin

    await process_message(event, config, plugin.get_plugin_data_dir())
