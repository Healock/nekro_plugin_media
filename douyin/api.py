"""抖音链接解析：短链跳转 + 分享页 `_ROUTER_DATA` 抓取。

主链路零签名、零登录 Cookie，不依赖任何第三方解析接口，也不依赖 Node/JS：

1. 短链（v.douyin.com / jx.douyin.com）经由一次 302 跳转拿到 aweme_id；
2. 请求 `www.iesdouyin.com/share/{video|note|slides}/{aweme_id}/` 分享页，
   作品数据由服务端内联在 HTML 的 `window._ROUTER_DATA` 中；
3. 解析出无水印播放直链 / 图集图片地址。

说明：实测抖音分享页在**无任何 Cookie** 时会返回空壳页面（缺少 `item_list`），
需要一枚**匿名设备 Cookie `ttwid`**（由 `ttwid.bytedance.com` 匿名注册发放，
不涉及登录态、不涉及签名算法）才会内联作品数据。

失败处理：本模块不再返回 ``None`` 静默吞掉错误，而是统一抛出
:class:`~nekro_plugin_media.download_kit.MediaDownloadError`，并区分
网络可重试、凭据失效（自动刷新一次）、作品永久不可用与解析失败。
"""

from __future__ import annotations

import time

import aiohttp
from nekro_agent.api import core

from ..download_kit import FailureKind, MediaDownloadError, translate_network_error
from .models import (
    DOUYIN_URL_REGEX,
    DouyinContext,
    extract_aweme_id,
    extract_router_data,
)

__all__ = [
    "COMMON_HEADERS",
    "DOUYIN_URL_REGEX",
    "SHORT_LINK_HEADERS",
    "fetch_douyin_context",
    "resolve_aweme_id",
]

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Referer": "https://www.iesdouyin.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

SHORT_LINK_HEADERS = {
    **COMMON_HEADERS,
    "Referer": "https://www.douyin.com/",
}

_SHARE_PAGE_TYPES = ("video", "note", "slides")

# 短链域名：需要跟随 302 才能拿到 aweme_id。
_SHORT_LINK_HOSTS = ("v.douyin.com", "jx.douyin.com")

# 匿名 ttwid 注册端点（无需登录，无签名）。
_TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
_TTWID_PAYLOAD = {
    "region": "cn",
    "aid": 1768,
    "needFid": False,
    "service": "www.iesdouyin.com",
    "migrate_info": {"ticket": "", "source": "node"},
    "cbUrlProtocol": "https",
    "union": True,
}
# 匿名 ttwid 有效期：过期后需重新注册（不能永久复用）。
_TTWID_TTL_SECONDS = 30 * 60

# 匿名 ttwid 进程内缓存，避免每条消息重复注册。
_ttwid_cache: str | None = None
_ttwid_expiry: float = 0.0


def _is_short_link(target: str) -> bool:
    return any(host in target for host in _SHORT_LINK_HOSTS)


def _invalidate_ttwid() -> None:
    """清空 ttwid 缓存，强制下次重新注册（用于凭据失效场景）。"""
    global _ttwid_cache, _ttwid_expiry
    _ttwid_cache = None
    _ttwid_expiry = 0.0


async def resolve_aweme_id(target: str) -> str:
    """将抖音链接解析为标准 aweme_id。

    长链可直接提取 ID；短链通过一次 HTTP 302 跳转，从 Location 中提取。
    解析失败时抛出 :class:`MediaDownloadError`（网络类可重试）。
    """
    if not _is_short_link(target):
        direct = extract_aweme_id(target)
        if direct is not None:
            return direct

    url = target if target.startswith(("http://", "https://")) else f"https://{target}"
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(headers=SHORT_LINK_HEADERS, timeout=timeout) as session:
            async with session.get(url, allow_redirects=False) as response:
                location = response.headers.get("Location", "")
                if response.status in (301, 302, 303, 307, 308) and location:
                    aweme_id = extract_aweme_id(location)
                else:
                    aweme_id = extract_aweme_id(str(response.url))
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        core.logger.warning(f"[Douyin] 短链解析异常：{exc}")
        raise translate_network_error(exc) from exc
    if not aweme_id:
        raise MediaDownloadError(FailureKind.PARSE, "无法从该链接解析出抖音作品 ID", detail=target)
    return aweme_id


async def _register_ttwid(session: aiohttp.ClientSession) -> str | None:
    """匿名注册 ttwid 设备 Cookie（无登录、无签名）。"""
    try:
        async with session.post(
            _TTWID_REGISTER_URL,
            json=_TTWID_PAYLOAD,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status != 200:
                return None
            cookies = response.headers.getall("Set-Cookie", [])
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        core.logger.warning(f"[Douyin] 匿名 ttwid 注册失败：{exc}")
        return None
    for cookie in cookies:
        if "ttwid=" in cookie:
            return cookie.split("ttwid=", 1)[1].split(";", 1)[0]
    return None


async def _get_ttwid(session: aiohttp.ClientSession) -> str | None:
    global _ttwid_cache, _ttwid_expiry
    if _ttwid_cache and time.time() < _ttwid_expiry:
        return _ttwid_cache
    _ttwid_cache = await _register_ttwid(session)
    _ttwid_expiry = time.time() + _TTWID_TTL_SECONDS if _ttwid_cache else 0.0
    return _ttwid_cache


async def _fetch_share_page(
    session: aiohttp.ClientSession, url: str, headers: dict[str, str]
) -> tuple[int, str | None]:
    """请求分享页，返回 (状态码, 正文)。网络类异常统一收敛为可重试错误。"""
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as response:
            status = response.status
            if status != 200:
                return status, None
            return status, await response.text()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise translate_network_error(exc) from exc


async def _try_share_pages(
    session: aiohttp.ClientSession,
    aweme_id: str,
    cookie: str | None,
) -> DouyinContext | None:
    """依次尝试三种分享页类型。

    - 命中作品数据 → 返回上下文；
    - 全部 404 → 抛永久失败（作品不存在/已删除）；
    - 401/403 → 抛鉴权失败（交由上层刷新 ttwid 后重试一次）；
    - 网络类错误 → 全部页面都失败时抛出可重试错误，否则忽略并继续尝试其它页型。
    """
    headers = dict(COMMON_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    network_error: MediaDownloadError | None = None
    not_found = 0
    for page_type in _SHARE_PAGE_TYPES:
        share_url = f"https://www.iesdouyin.com/share/{page_type}/{aweme_id}/"
        try:
            status, html = await _fetch_share_page(session, share_url, headers)
        except MediaDownloadError as exc:
            network_error = network_error or exc
            continue
        if status in (401, 403):
            raise MediaDownloadError(FailureKind.AUTH, "抖音访问凭证已失效，正在刷新", detail=str(status))
        if status == 404:
            not_found += 1
            continue
        if html is None:
            continue
        router_data = extract_router_data(html)
        if router_data is None:
            continue
        context = DouyinContext.from_router_data(router_data)
        if context is not None:
            return context
    if network_error is not None:
        raise network_error
    if not_found == len(_SHARE_PAGE_TYPES):
        raise MediaDownloadError(FailureKind.UNAVAILABLE, "该抖音作品不存在或已被删除")
    return None


async def fetch_douyin_context(aweme_id: str) -> DouyinContext:
    """请求分享页并解析作品上下文。

    使用匿名 ttwid（带缓存与有效期）；当页面表现为凭据失效（401/403 或空壳页）时，
    清空缓存并重新注册，之后**仅重试一次**。最终仍失败则抛出带原因的异常。
    """
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(headers=COMMON_HEADERS, timeout=timeout) as session:
        for attempt in (0, 1):
            ttwid = await _get_ttwid(session)
            try:
                context = await _try_share_pages(session, aweme_id, f"ttwid={ttwid}" if ttwid else None)
            except MediaDownloadError as exc:
                if exc.kind is FailureKind.AUTH and attempt == 0:
                    _invalidate_ttwid()
                    continue
                raise
            if context is not None:
                return context
            if attempt == 0:
                # 空 body / 风控壳页：视为凭据可疑，刷新后仅重试一次。
                _invalidate_ttwid()

    raise MediaDownloadError(
        FailureKind.PARSE,
        "无法解析该抖音作品（可能为私密、已删除或触发风控）",
        detail=aweme_id,
    )
