"""抖音游客签名解析，改编自 ImNotBird/astrbot_plugin_video_resolver。"""

from __future__ import annotations

import asyncio
import random
import string
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp
from yarl import URL

from ..download_kit import FailureKind, MediaDownloadError, translate_network_error
from .abogus import ABogus, BrowserFingerprintGenerator, ensure_dependency
from .models import DouyinContext, is_safe_media_url
from .websign import encode_pairs, sign

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)
_GUEST_URL = "https://live.douyin.com/"
_DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
_SESSION_TTL = 30 * 60


@dataclass(frozen=True, slots=True)
class GuestSession:
    ttwid: str
    uifid: str

    @property
    def cookie(self) -> str:
        return f"ttwid={self.ttwid}; UIFID_TEMP={self.uifid};"


@dataclass(frozen=True, slots=True)
class GuestRequest:
    url: str
    headers: dict[str, str]


def _referer_for(aweme_id: str, source_url: str | None) -> str:
    if source_url:
        try:
            hostname = (urlparse(source_url).hostname or "").lower()
        except ValueError:
            hostname = ""
        if hostname == "douyin.com" or hostname.endswith(".douyin.com"):
            return source_url
    return f"https://www.douyin.com/video/{aweme_id}"


def build_guest_request(
    aweme_id: str,
    session: GuestSession,
    source_url: str | None = None,
    *,
    timestamp: int | None = None,
) -> GuestRequest:
    token_alphabet = string.ascii_letters + string.digits + "-_"
    ms_token = "".join(random.choices(token_alphabet, k=184))
    params = [
        ("device_platform", "webapp"),
        ("aid", "6383"),
        ("channel", "channel_pc_web"),
        ("pc_client_type", "1"),
        ("publish_video_strategy_type", "2"),
        ("pc_libra_divert", "Windows"),
        ("version_code", "290100"),
        ("version_name", "29.1.0"),
        ("cookie_enabled", "true"),
        ("screen_width", "1920"),
        ("screen_height", "1080"),
        ("browser_language", "zh-CN"),
        ("browser_platform", "Win32"),
        ("browser_name", "Edge"),
        ("browser_version", "130.0.0.0"),
        ("browser_online", "true"),
        ("engine_name", "Blink"),
        ("engine_version", "130.0.0.0"),
        ("os_name", "Windows"),
        ("os_version", "10"),
        ("cpu_core_num", "12"),
        ("device_memory", "8"),
        ("platform", "PC"),
        ("downlink", "10"),
        ("effective_type", "4g"),
        ("round_trip_time", "100"),
        ("msToken", ms_token),
        ("aweme_id", aweme_id),
        ("uifid", session.uifid),
    ]
    query = encode_pairs(params)
    fingerprint = BrowserFingerprintGenerator.generate_fingerprint("Edge")
    abogus = ABogus(fp=fingerprint, user_agent=DESKTOP_USER_AGENT).generate_abogus(
        query, ""
    )[1]
    signed = sign(
        encode_pairs([*params, ("a_bogus", abogus)]),
        session.uifid,
        timestamp=timestamp,
    )
    return GuestRequest(
        url=f"{_DETAIL_URL}?{signed.query}",
        headers={
            "Referer": _referer_for(aweme_id, source_url),
            "uifid": session.uifid,
            "x-secsdk-web-signature": signed.signature,
            "x-secsdk-web-expire": signed.timestamp,
        },
    )


def _context_from_detail(detail: dict, session: GuestSession) -> DouyinContext:
    context = DouyinContext.from_item(detail)
    media_urls = [context.video_url, *context.image_urls]
    if not any(media_urls):
        raise MediaDownloadError(FailureKind.PARSE, "作品详情没有可下载媒体")
    if any(url and not is_safe_media_url(url) for url in media_urls):
        raise MediaDownloadError(FailureKind.PARSE, "抖音签名接口返回了无效媒体地址")
    context.download_cookie = session.cookie
    return context


class DouyinSignedGuestAPI:
    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._session: GuestSession | None = None
        self._session_expiry = 0.0
        self._session_lock = asyncio.Lock()

    async def _create_session(self) -> GuestSession:
        headers = {
            "User-Agent": DESKTOP_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as client:
            async with client.get(_GUEST_URL) as response:
                response.raise_for_status()
            cookies = client.cookie_jar.filter_cookies(URL(_GUEST_URL))
        ttwid = cookies.get("ttwid")
        uifid = cookies.get("UIFID_TEMP")
        if not ttwid or not uifid:
            raise MediaDownloadError(
                FailureKind.AUTH, "抖音游客身份初始化失败（缺少匿名 Cookie）"
            )
        return GuestSession(ttwid=ttwid.value, uifid=uifid.value)

    async def _get_session(self, *, refresh: bool = False) -> GuestSession:
        async with self._session_lock:
            if refresh or self._session is None or time.monotonic() >= self._session_expiry:
                self._session = await self._create_session()
                self._session_expiry = time.monotonic() + _SESSION_TTL
            return self._session

    async def fetch_context(
        self, aweme_id: str, source_url: str | None = None
    ) -> DouyinContext:
        last_error = "empty response"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        for refresh in (False, True):
            try:
                session = await self._get_session(refresh=refresh)
                await asyncio.to_thread(ensure_dependency)
                request = await asyncio.to_thread(
                    build_guest_request, aweme_id, session, source_url
                )
                headers = {
                    "User-Agent": DESKTOP_USER_AGENT,
                    "Cookie": session.cookie,
                    **request.headers,
                }
                async with aiohttp.ClientSession(timeout=timeout) as client:
                    async with client.get(request.url, headers=headers) as response:
                        status = response.status
                        if status != 200:
                            last_error = f"HTTP {status}"
                            continue
                        payload = await response.json(content_type=None)
            except asyncio.CancelledError:
                raise
            except MediaDownloadError as exc:
                if refresh:
                    raise
                last_error = exc.kind.value
                continue
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                last_error = type(exc).__name__
                if refresh:
                    raise translate_network_error(exc) from exc
                continue

            detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
            if not isinstance(detail, dict) or not detail:
                status_code = payload.get("status_code") if isinstance(payload, dict) else None
                last_error = f"missing aweme_detail (status_code={status_code})"
                continue
            return _context_from_detail(detail, session)
        raise MediaDownloadError(
            FailureKind.PARSE,
            "抖音游客签名解析失败，已尝试刷新匿名身份",
            detail=last_error,
        )


_SIGNED_GUEST_API = DouyinSignedGuestAPI()


async def fetch_signed_guest_context(
    aweme_id: str, source_url: str | None = None
) -> DouyinContext:
    return await _SIGNED_GUEST_API.fetch_context(aweme_id, source_url)
