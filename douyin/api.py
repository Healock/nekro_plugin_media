"""抖音链接解析：短链跳转 + 分享页 `_ROUTER_DATA` 抓取。

主链路零签名、零登录 Cookie，不依赖任何第三方解析接口，也不依赖 Node/JS：

1. 短链（v.douyin.com / jx.douyin.com）经由一次 302 跳转拿到 aweme_id；
2. 请求 `www.iesdouyin.com/share/{video|note|slides}/{aweme_id}/` 分享页，
   作品数据由服务端内联在 HTML 的 `window._ROUTER_DATA` 中；
3. 解析出无水印播放直链 / 图集图片地址。

说明：实测抖音分享页在**无任何 Cookie** 时会返回空壳页面（缺少 `item_list`），
需要一枚**匿名设备 Cookie `ttwid`**（由 `ttwid.bytedance.com` 匿名注册发放，
不涉及登录态、不涉及签名算法）才会内联作品数据。因此这里先以“无 Cookie”请求一次，
失败后再匿名注册 ttwid 重试——两条路径均无签名、无登录凭证。
"""

from __future__ import annotations

import aiohttp
from nekro_agent.api import core

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

# 匿名 ttwid 进程内缓存，避免每条消息重复注册。
_ttwid_cache: str | None = None


def _is_short_link(target: str) -> bool:
    return any(host in target for host in _SHORT_LINK_HOSTS)


async def resolve_aweme_id(target: str) -> str | None:
    """将抖音链接解析为标准 aweme_id。

    长链可直接提取 ID；短链通过一次 HTTP 302 跳转，从 Location 中提取。
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
                    return extract_aweme_id(location)
                return extract_aweme_id(str(response.url))
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        core.logger.error(f"[Douyin] 短链解析异常：{exc}")
        return None


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
    global _ttwid_cache
    if _ttwid_cache:
        return _ttwid_cache
    _ttwid_cache = await _register_ttwid(session)
    return _ttwid_cache


async def _fetch_html(session: aiohttp.ClientSession, url: str, headers: dict[str, str]) -> str | None:
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as response:
            if response.status != 200:
                return None
            return await response.text()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        core.logger.warning(f"[Douyin] 分享页请求失败 {url}：{exc}")
        return None


async def _try_share_pages(
    session: aiohttp.ClientSession,
    aweme_id: str,
    cookie: str | None,
) -> DouyinContext | None:
    headers = dict(COMMON_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    for page_type in _SHARE_PAGE_TYPES:
        share_url = f"https://www.iesdouyin.com/share/{page_type}/{aweme_id}/"
        html = await _fetch_html(session, share_url, headers)
        if not html:
            continue
        router_data = extract_router_data(html)
        if router_data is None:
            continue
        context = DouyinContext.from_router_data(router_data)
        if context is not None:
            return context
    return None


async def fetch_douyin_context(aweme_id: str) -> DouyinContext | None:
    """请求分享页并解析作品上下文。

    先以无 Cookie 请求；若分享页未内联作品数据，则匿名注册 ttwid 后重试。
    """
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(headers=COMMON_HEADERS, timeout=timeout) as session:
        context = await _try_share_pages(session, aweme_id, cookie=None)
        if context is not None:
            return context

        ttwid = await _get_ttwid(session)
        if not ttwid:
            core.logger.warning(f"[Douyin] 无 Cookie 且 ttwid 注册失败，无法解析：{aweme_id}")
            return None
        context = await _try_share_pages(session, aweme_id, cookie=f"ttwid={ttwid}")

    if context is None:
        core.logger.warning(f"[Douyin] 无法从分享页解析作品：{aweme_id}")
    return context
