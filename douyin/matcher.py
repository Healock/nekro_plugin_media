"""抖音消息拦截：识别分享链接并把消息重写为可分析的媒体段落。

与主流程的耦合方式与 Bilibili 子模块完全一致——仅重写消息内容：
- 视频帖：追加 `MessageSegment.video(file=<本地待下载路径>)`，由主流程媒体拦截器接管分析；
- 图文帖：直接追加图片段落，不触发媒体分析流程。

失败处理：解析、短链与下载失败都不再静默——解析类失败立即向聊天发送可读提示，
下载类失败在后台任务结束时同样发送提示，避免用户长时间无反馈。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from nekro_agent.api import core
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from ..download_kit import MediaDownloadError
from .api import DOUYIN_URL_REGEX
from .downloader import auto_clean_temp_dir, find_cached_file, schedule_download
from .notify import notify_user, watch_download
from .resolver import resolve_context

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


async def _resolve_context(target_url: str) -> tuple[str, Any]:
    from .. import config
    return await resolve_context(target_url, config)


async def process_message(
    event: MessageEvent, config: object, data_dir: Path, bot: Bot | None = None
) -> None:
    target_url = extract_target_url(event.message)
    if not target_url:
        return

    try:
        aweme_id, context = await _resolve_context(target_url)
    except MediaDownloadError as exc:
        core.logger.warning(f"[Douyin] 解析失败（{exc.kind.value}）：{exc.detail}")
        await notify_user(bot, event, f"【抖音解析失败】{exc.user_message()}")
        return
    except Exception as exc:  # noqa: BLE001 - 兜底防漏，仍要告知用户
        core.logger.error(f"[Douyin] 解析异常：{exc}")
        await notify_user(bot, event, "【抖音解析失败】解析过程中发生未知错误，请稍后重试。")
        return

    max_minutes = _config_int(config, "douyin_max_duration", 30)
    if not context.is_gallery and max_minutes > 0 and context.duration > max_minutes * 60:
        core.logger.warning(f"[Douyin] 视频超过时长限制：{aweme_id}")
        await notify_user(bot, event, f"【抖音视频超时长】该视频超过 {max_minutes} 分钟限制，已跳过处理。")
        return

    temp_dir = data_dir / "douyin"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        core.logger.error(f"[Douyin] 临时目录不可用：{exc}")
        await notify_user(bot, event, "【抖音解析失败】本地临时目录不可写，请检查磁盘空间或权限。")
        return
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
                    task = schedule_download(
                        aweme_id,
                        media_path,
                        context.video_url,
                        str(getattr(config, "douyin_download_quality", "720p")),
                        data_dir,
                        watermark_url=context.video_url_watermarked,
                        timeout=float(_config_int(config, "douyin_download_timeout", 180)),
                        cookie=context.download_cookie,
                    )
                    watch_download(task, aweme_id, bot, event)
                    try:
                        await task
                    except Exception:
                        # watch_download 已负责一次用户可见的失败提示；失败时不把不存在的
                        # 占位路径交给 Media matcher，也不改写原始消息。
                        return
                    cached = find_cached_file(temp_dir, aweme_id)
                    if cached is None:
                        core.logger.warning(f"[Douyin] 下载任务完成但未找到有效成品：{aweme_id}")
                        return
                    media_path = cached
                replacement.append(MessageSegment.video(file=str(media_path.absolute())))
        else:
            replacement.append(segment)
    if replaced:
        event.message.clear()
        event.message.extend(replacement)


douyin_interceptor = on_message(priority=9, block=False)


@douyin_interceptor.handle()
async def handle_douyin_penetration(bot: Bot, event: MessageEvent) -> None:
    from .. import config, plugin

    await process_message(event, config, plugin.get_plugin_data_dir(), bot=bot)
