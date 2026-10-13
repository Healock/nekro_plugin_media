"""ucmao/media-parser compatible Douyin API client."""

from __future__ import annotations

from typing import Any

import aiohttp

from ..download_kit import FailureKind, MediaDownloadError, translate_network_error
from .models import DouyinContext

__all__ = ["fetch_media_parser_context", "is_media_parser_enabled"]


def _base_url(config: object) -> str:
    return str(getattr(config, "douyin_api_base_url", "") or "").strip().rstrip("/")


def _headers(config: object) -> dict[str, str]:
    key = str(getattr(config, "douyin_api_key", "") or "").strip()
    return {"X-API-Key": key} if key else {}


async def _json(response: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        payload = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as exc:
        raise MediaDownloadError(FailureKind.PARSE, "抖音 API 返回了无效响应") from exc
    if not isinstance(payload, dict):
        raise MediaDownloadError(FailureKind.PARSE, "抖音 API 返回了无效响应")
    return payload


def _failure_kind(status: int, payload: dict[str, Any]) -> FailureKind | None:
    if status in {401, 403}:
        return FailureKind.AUTH
    if status >= 500:
        return FailureKind.SERVER
    if status >= 400:
        return FailureKind.PARSE
    if payload.get("succ") is False or payload.get("success") is False:
        return FailureKind.PARSE
    if payload.get("retcode") not in (None, 200):
        return FailureKind.PARSE
    if payload.get("code") not in (None, 200):
        return FailureKind.PARSE
    return None


async def fetch_media_parser_context(target_url: str, config: object) -> DouyinContext:
    """Call media-parser's standard parse endpoint and map its response."""
    base_url = _base_url(config)
    if not base_url:
        raise MediaDownloadError(FailureKind.PARSE, "未配置抖音 API 地址")
    timeout_seconds = max(1, int(getattr(config, "douyin_api_timeout", 45) or 45))
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    try:
        async with aiohttp.ClientSession(headers=_headers(config), timeout=timeout) as session:
            async with session.post(
                f"{base_url}/api/v1/parse", json={"url": target_url}
            ) as response:
                payload = await _json(response)
                kind = _failure_kind(response.status, payload)
                if kind is not None:
                    raise MediaDownloadError(kind, "抖音 API 解析失败")
    except MediaDownloadError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise translate_network_error(exc) from exc

    data = payload.get("data")
    if not isinstance(data, dict):
        raise MediaDownloadError(FailureKind.PARSE, "抖音 API 未返回作品数据")
    context = DouyinContext.from_media_parser_data(data)
    if not context.video_url and not context.image_urls:
        raise MediaDownloadError(FailureKind.PARSE, "抖音 API 未返回可用媒体")
    return context


def is_media_parser_enabled(config: object) -> bool:
    return (
        str(getattr(config, "douyin_api_provider", "native") or "native") == "media_parser"
        and bool(_base_url(config))
    )
