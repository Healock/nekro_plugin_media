"""抖音解析后端选择与有限重试。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from nekro_agent.api import core

from ..download_kit import MediaDownloadError, run_with_retry
from .api import fetch_douyin_context, resolve_aweme_id
from .dtk import fetch_dtk_context, is_dtk_enabled
from .media_parser import fetch_media_parser_context, is_media_parser_enabled
from .signed_guest import fetch_signed_guest_context

_MAX_RETRIES = 2
_RETRY_BACKOFF = (1.0, 3.0)


async def _retry(operation: Callable[[], Awaitable[Any]]) -> Any:
    def on_retry(exc: MediaDownloadError, attempt: int, delay: float) -> None:
        core.logger.warning(f"[Douyin] 解析第 {attempt} 次重试（{delay:.0f}s 后）：{exc.kind.value}")

    return await run_with_retry(operation, max_retries=_MAX_RETRIES, backoff=_RETRY_BACKOFF, on_retry=on_retry)


async def resolve_context(target_url: str, config: object) -> tuple[str, Any]:
    """解析作品 ID 后调用已配置后端，内置路径优先游客签名接口。

    外部后端失败后回退到游客签名接口，再回退分享页解析。各解析路径分别有限重试。
    """
    aweme_id = await _retry(lambda: resolve_aweme_id(target_url))
    if is_media_parser_enabled(config):
        try:
            context = await _retry(lambda: fetch_media_parser_context(target_url, config))
            return aweme_id, context
        except MediaDownloadError as exc:
            core.logger.warning(f"[Douyin] media-parser 后端失败（{exc.kind.value}），回退内置解析")
    elif is_dtk_enabled(config):
        try:
            context = await _retry(lambda: fetch_dtk_context(target_url, config))
            return aweme_id, context
        except MediaDownloadError as exc:
            core.logger.warning(f"[Douyin] DTK 后端失败（{exc.kind.value}），回退内置解析")
    try:
        context = await _retry(
            lambda: fetch_signed_guest_context(aweme_id, target_url)
        )
        return aweme_id, context
    except MediaDownloadError as exc:
        core.logger.warning(
            f"[Douyin] 游客签名解析失败（{exc.kind.value}），回退分享页解析"
        )
    except Exception as exc:  # noqa: BLE001 - optional dynamic signer may fail to initialize
        core.logger.warning(
            f"[Douyin] 游客签名解析初始化失败（{type(exc).__name__}），回退分享页解析"
        )
    return aweme_id, await _retry(lambda: fetch_douyin_context(aweme_id))
