from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from nekro_agent.api import core
from nekro_agent.api.plugin import ConfigBase, NekroPlugin
from pydantic import Field


plugin = NekroPlugin(
    name="媒体阅读器",
    module_name="nekro_plugin_media",
    description="处理 OneBot 音视频与 Bilibili 分享链接，按需调用 Gemini 提取媒体内容。",
    version="1.1.0",
    author="Healock",
    url="https://github.com/Healock/nekro_plugin_media",
    support_adapter=["onebot_v11"],
)


@plugin.mount_config()
class MediaConfig(ConfigBase):
    GEMINI_API_KEY: str = Field(default="", title="Google Gemini API Key", json_schema_extra={"is_secret": True})
    GEMINI_MODEL: str = Field(default="gemini-3-flash-preview", title="模型名称")
    download_quality: Literal["360p", "480p", "720p", "best"] = Field(default="480p", title="Bilibili 下载画质")
    max_duration: int = Field(default=30, title="Bilibili 最大时长（分钟）")
    desc_char_limit: int = Field(default=150, title="Bilibili 简介截断长度")
    comment_count: int = Field(default=3, title="Bilibili 热评数量")
    comment_char_limit: int = Field(default=100, title="Bilibili 单条热评截断长度")
    NAPCAT_TEMP_DIR: str = Field(default="/app/.config/QQ/NapCat/temp", title="NapCat 临时目录")


config = plugin.get_config(MediaConfig)
generated_temp_files: set[Path] = set()
background_tasks: set[asyncio.Task[None]] = set()

core.logger.info("[Media] 插件已加载")

# 子模块只在入口对象和配置类定义完成后加载，避免循环初始化。
from .cache import MEDIA_CACHE
from . import commands, lifecycle, matcher, sandbox  # noqa: E402,F401
from .bilibili import registration as _bilibili_registration  # noqa: E402,F401

__all__ = ["MEDIA_CACHE", "MediaConfig", "background_tasks", "config", "generated_temp_files", "plugin"]
