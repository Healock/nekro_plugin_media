from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from nonebot import get_bot
from nekro_agent.api import message
from nekro_agent.api.plugin import SandboxMethodType
from nekro_agent.api.schemas import AgentCtx

from . import MEDIA_CACHE, MediaConfig, background_tasks, generated_temp_files, plugin
from .parser import MediaParser
from .types import MediaCacheEntry, MediaStatus, MediaType


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    "analyze_media_file",
    "提取指定音视频内容；语音同步返回，视频通过后台通知返回。",
)
async def analyze_media_file(_ctx: AgentCtx, media_id: str, message: str = "") -> str:
    """分析缓存中的音频或视频媒体。

    Args:
        media_id (str): 媒体消息创建的句柄 ID，格式为媒体通知中提供的句柄。
        message (str, optional): 用户希望重点关注的内容或问题。默认为空，表示进行常规分析。

    Returns:
        str: 音频会同步返回分析结果；视频会返回已提交后台任务的状态，任务完成后向原聊天推送结果。

    Raises:
        ValueError: 媒体句柄不存在时抛出。
        Exception: 音频分析失败时保留原始异常并抛出；视频分析失败会通过后台通知报告。
    """
    clean_id = str(media_id).strip(" '\"\n\t")
    entry = MEDIA_CACHE.get_entry(clean_id)
    if entry is None:
        raise ValueError(f"【句柄不存在】{clean_id}，请让用户重新发送原始媒体。")
    if entry.status is MediaStatus.DONE:
        return entry.result or ""
    if entry.status is MediaStatus.PROCESSING:
        return "【处理中】目标资源正在处理，请等待后续通知。"
    config = plugin.get_config(MediaConfig)
    parser = MediaParser(config.GEMINI_API_KEY, config.GEMINI_MODEL, _temp_dir(), generated_temp_files)
    if entry.type is MediaType.AUDIO:
        entry.status = MediaStatus.PROCESSING
        try:
            result = await parser.process(
                entry, message, _get_bot(entry.bot_id), napcat_temp_dir=Path(config.NAPCAT_TEMP_DIR)
            )
        except Exception:
            entry.status = MediaStatus.ERROR
            raise
        entry.status = MediaStatus.DONE
        entry.result = result
        return result

    entry.status = MediaStatus.PROCESSING
    entry.chat_key = _ctx.from_chat_key
    entry.agent_message = message
    bot = _get_bot(entry.bot_id)
    task = asyncio.create_task(
        _run_video(parser, clean_id, entry, bot, Path(config.NAPCAT_TEMP_DIR)), name=f"media-{clean_id}"
    )
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return "【视频解析已提交】任务已进入后台队列，完成后将推送结果。"


async def _run_video(parser: MediaParser, media_id: str, entry: MediaCacheEntry, bot: Any, napcat_temp_dir: Path) -> None:
    async def complete(identifier: str, result: str) -> None:
        if entry.chat_key:
            await message.push_system(
                chat_key=entry.chat_key,
                message=f"【媒体解析完成】\n句柄：{identifier}\n【解析结果】\n{result}",
                trigger_agent=True,
            )

    async def failed(identifier: str, exc: Exception) -> None:
        if entry.chat_key:
            await message.push_system(
                chat_key=entry.chat_key,
                message=f"【媒体解析失败】\n句柄：{identifier}。\n原因：{exc}",
                trigger_agent=True,
            )

    await parser.process_video_background(
        media_id, entry, bot, complete, failed, napcat_temp_dir=napcat_temp_dir
    )


def _get_bot(bot_id: str | None) -> Any:
    if not bot_id:
        return None
    return get_bot(bot_id)


def _temp_dir() -> Path:
    return plugin.get_plugin_data_dir() / "media"
