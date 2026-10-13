from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from nekro_agent.api import core
from nekro_agent.api.plugin import ConfigBase, ExtraField, NekroPlugin
from pydantic import Field


plugin = NekroPlugin(
    name="媒体阅读器",
    module_name="nekro_plugin_media",
    description="处理 OneBot 音视频与 Bilibili / 抖音分享链接，按需调用 Nekro 模型组提取媒体内容。",
    version="1.4.0",
    author="Healock",
    url="https://github.com/Healock/nekro_plugin_media",
    support_adapter=["onebot_v11"],
)


@plugin.mount_config()
class MediaConfig(ConfigBase):
    MEDIA_MODEL_GROUP: str = Field(
        default="",
        title="媒体分析模型组",
        description="选择用于音频和视频分析的聊天模型组。",
        json_schema_extra=ExtraField(ref_model_groups=True, model_type="chat", required=True).model_dump(),
    )
    GEMINI_API_KEY: str = Field(
        default="",
        title="旧版 Gemini API Key",
        description="兼容旧配置；设置媒体分析模型组后不再使用。",
        json_schema_extra={"is_secret": True},
    )
    GEMINI_MODEL: str = Field(
        default="gemini-3-flash-preview",
        title="旧版 Gemini 模型名称",
        description="兼容旧配置；设置媒体分析模型组后不再使用。",
    )
    download_quality: Literal["360p", "480p", "720p", "best"] = Field(default="480p", title="Bilibili 下载画质")
    max_duration: int = Field(default=30, title="Bilibili 最大时长（分钟）")
    desc_char_limit: int = Field(default=150, title="Bilibili 简介截断长度")
    comment_count: int = Field(default=3, title="Bilibili 热评数量")
    comment_char_limit: int = Field(default=100, title="Bilibili 单条热评截断长度")
    douyin_download_quality: Literal["best", "1080p", "720p", "540p", "360p"] = Field(
        default="720p", title="抖音下载画质"
    )
    douyin_max_duration: int = Field(default=30, title="抖音最大时长（分钟）")
    douyin_desc_char_limit: int = Field(default=150, title="抖音文案截断长度")
    douyin_download_timeout: int = Field(default=180, title="抖音下载总超时（秒）")
    douyin_api_provider: Literal["native", "dtk"] = Field(
        default="native", title="抖音解析后端", description="native 使用内置分享页解析；dtk 使用可配置的 DTK 兼容 API。"
    )
    douyin_api_base_url: str = Field(
        default="", title="抖音 API 地址", description="DTK API 根地址，例如 https://demo.douyin.wtf；留空时使用内置解析。"
    )
    douyin_api_key: str = Field(
        default="",
        title="抖音 API Key",
        description="可选。自建或托管 DTK 服务的 X-API-Key。",
        json_schema_extra={"is_secret": True},
    )
    douyin_api_demo_login: bool = Field(
        default=False,
        title="启用 DTK Demo 登录",
        description="无 API Key 时通过服务的 /api/v1/auth/demo 登录，仅适用于明确支持该流程的测试服务。",
    )
    douyin_api_timeout: int = Field(default=45, title="抖音 API 超时（秒）")
    NAPCAT_TEMP_DIR: str = Field(default="/app/.config/QQ/NapCat/temp", title="NapCat 临时目录")


config = plugin.get_config(MediaConfig)
generated_temp_files: set[Path] = set()
background_tasks: set[asyncio.Task[None]] = set()

core.logger.info("[Media] 插件已加载")

# 子模块只在入口对象和配置类定义完成后加载，避免循环初始化。
from .cache import MEDIA_CACHE
from . import commands, lifecycle, matcher, sandbox  # noqa: E402,F401
from .bilibili import registration as _bilibili_registration  # noqa: E402,F401
from .douyin import registration as _douyin_registration  # noqa: E402,F401

__all__ = ["MEDIA_CACHE", "MediaConfig", "background_tasks", "config", "generated_temp_files", "plugin"]
