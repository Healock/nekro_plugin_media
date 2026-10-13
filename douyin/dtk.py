"""DTK 兼容抖音解析 API 客户端。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from ..download_kit import FailureKind, MediaDownloadError, translate_network_error
from .models import DouyinContext

__all__ = ["fetch_dtk_context", "is_dtk_enabled"]


def _base_url(config: object) -> str:
    return str(getattr(config, "douyin_api_base_url", "") or "").strip().rstrip("/")


def _api_headers(config: object) -> dict[str, str]:
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


def _failure_kind_for_response(status: int, payload: dict[str, Any]) -> FailureKind | None:
    if status in {401, 403}:
        return FailureKind.AUTH
    if status >= 500:
        return FailureKind.SERVER
    if status >= 400:
        return FailureKind.PARSE
    if payload.get("success"):
        return None
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else "UNKNOWN"
    return FailureKind.AUTH if code in {"UNAUTHENTICATED", "FORBIDDEN"} else FailureKind.PARSE


def _raise_for_response(status: int, payload: dict[str, Any]) -> None:
    kind = _failure_kind_for_response(status, payload)
    if kind is None:
        return
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else f"HTTP_{status}"
    raise MediaDownloadError(kind, f"抖音 API 解析失败（{code}）")


async def _login_demo(session: aiohttp.ClientSession, base_url: str) -> None:
    try:
        async with session.get(f"{base_url}/api/v1/auth/demo") as response:
            demo = await _json(response)
        data = demo.get("data")
        if not demo.get("success") or not isinstance(data, dict):
            raise MediaDownloadError(FailureKind.AUTH, "抖音 API Demo 账号不可用")
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise MediaDownloadError(FailureKind.AUTH, "抖音 API Demo 账号配置不完整")
        async with session.post(
            f"{base_url}/api/v1/auth/login", json={"username": username, "password": password}
        ) as response:
            login = await _json(response)
        if not login.get("success"):
            raise MediaDownloadError(FailureKind.AUTH, "抖音 API Demo 登录失败")
    except MediaDownloadError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise translate_network_error(exc) from exc


async def fetch_dtk_context(target_url: str, config: object) -> DouyinContext:
    """通过 DTK 兼容接口解析作品，并映射为插件内部上下文。"""
    base_url = _base_url(config)
    if not base_url:
        raise MediaDownloadError(FailureKind.PARSE, "未配置抖音 API 地址")
    timeout_seconds = max(1, int(getattr(config, "douyin_api_timeout", 45) or 45))
    headers = _api_headers(config)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    # unsafe 仅用于兼容测试服务在本地/非标准域名下发放的会话 Cookie；不放宽媒体 URL 校验。
    async with aiohttp.ClientSession(
        headers=headers, timeout=timeout, cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        if not headers and bool(getattr(config, "douyin_api_demo_login", False)):
            await _login_demo(session, base_url)
        try:
            # DTK 服务端目前将 wait 限制在 30 秒；客户端总超时仍可更长。
            wait_seconds = min(timeout_seconds, 30)
            async with session.post(
                f"{base_url}/api/v1/parse?wait={wait_seconds}", json={"url": target_url}
            ) as response:
                try:
                    payload = await _json(response)
                except MediaDownloadError:
                    kind = _failure_kind_for_response(response.status, {})
                    if kind is None:
                        raise
                    raise MediaDownloadError(kind, f"抖音 API 解析失败（HTTP_{response.status}）") from None
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise translate_network_error(exc) from exc
        _raise_for_response(response.status, payload)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MediaDownloadError(FailureKind.PARSE, "抖音 API 未返回作品数据")
        context = DouyinContext.from_dtk_data(data)
        await _validate_public_media_hosts(context)
        return context


async def _validate_public_media_hosts(context: DouyinContext) -> None:
    """拒绝解析到非公网 IP 的媒体主机，缩小外部 API 引入的 SSRF 风险。"""
    urls = [context.video_url, *context.image_urls]
    hosts: set[tuple[str, int]] = set()
    for url in urls:
        if not url:
            continue
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            raise MediaDownloadError(FailureKind.PARSE, "抖音 API 返回了无效媒体地址")
        hosts.add((host, parsed.port or (443 if parsed.scheme == "https" else 80)))
    loop = asyncio.get_running_loop()
    for host, port in hosts:
        try:
            records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, ValueError) as exc:
            raise MediaDownloadError(FailureKind.NETWORK, "无法解析抖音媒体服务器地址") from exc
        if not records or any(not ipaddress.ip_address(record[4][0]).is_global for record in records):
            raise MediaDownloadError(FailureKind.PARSE, "抖音 API 返回了非公网媒体地址")


def is_dtk_enabled(config: object) -> bool:
    return str(getattr(config, "douyin_api_provider", "native") or "native") == "dtk" and bool(_base_url(config))
