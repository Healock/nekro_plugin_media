from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class MediaModelConfig:
    model: str
    api_key: str
    base_url: str
    proxy_url: str
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    extra_body: str | None = None
    native_gemini: bool = False


def resolve_model_config(config: Any) -> MediaModelConfig:
    """解析模型组配置；仅在模型组不可用时回退到旧版 Gemini 配置。"""
    group_name = str(getattr(config, "MEDIA_MODEL_GROUP", "")).strip()
    if group_name:
        try:
            group = _get_model_group_info(group_name)
        except KeyError as exc:
            raise ValueError(f"媒体分析模型组不存在：{group_name}") from exc
        if not group.CHAT_MODEL:
            raise ValueError(f"媒体分析模型组未配置聊天模型：{group_name}")
        base_url = str(group.BASE_URL or "").strip()
        if not base_url:
            raise ValueError(f"媒体分析模型组未配置 API 地址：{group_name}")
        return MediaModelConfig(
            model=group.CHAT_MODEL,
            api_key=group.API_KEY,
            base_url=base_url,
            proxy_url=group.CHAT_PROXY,
            temperature=group.TEMPERATURE,
            top_p=group.TOP_P,
            top_k=group.TOP_K,
            extra_body=group.EXTRA_BODY,
            native_gemini=_is_native_gemini_url(base_url),
        )

    api_key = str(getattr(config, "GEMINI_API_KEY", "")).strip()
    if not api_key:
        raise ValueError("未配置媒体分析模型组，也未配置旧版 GEMINI_API_KEY")
    return MediaModelConfig(
        model=str(getattr(config, "GEMINI_MODEL", "gemini-3-flash-preview")),
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com",
        proxy_url="",
        native_gemini=True,
    )


def has_model_config(config: Any) -> bool:
    """判断插件是否至少存在一个可用的媒体分析配置。"""
    group_name = str(getattr(config, "MEDIA_MODEL_GROUP", "")).strip()
    if group_name:
        try:
            group = _get_model_group_info(group_name)
        except KeyError:
            return False
        return bool(group.CHAT_MODEL)
    return bool(str(getattr(config, "GEMINI_API_KEY", "")).strip())


def _is_native_gemini_url(base_url: str) -> bool:
    return "generativelanguage.googleapis.com" in base_url.casefold() and "/openai" not in base_url.casefold()


def _get_model_group_info(group_name: str) -> Any:
    from nekro_agent.api import core

    return core.config.get_model_group_info(group_name)
