from __future__ import annotations

import re
from typing import Any

import aiohttp

from nekro_agent.api import core

from .models import BilibiliComment, BilibiliContext, BilibiliViewData

BILI_REGEX = r"(?:b23\.tv/[a-zA-Z0-9]+|bilibili\.com/video/BV[a-zA-Z0-9]+)"
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _extract_bvid(value: str) -> str | None:
    match = re.search(r"BV[a-zA-Z0-9]+", value)
    return match.group(0) if match else None


async def resolve_bvid(target_path: str) -> str | None:
    """跟随短链或视频链接并提取标准 BVID。"""
    url = target_path if target_path.startswith(("http://", "https://")) else f"https://{target_path}"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(headers=COMMON_HEADERS, timeout=timeout) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    real_url = response.headers.get("Location", "")
                else:
                    real_url = str(response.url)
        return _extract_bvid(real_url)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        core.logger.error(f"[Bilibili] 链路解析异常：{exc}")
        return None


def _read_api_data(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _config_value(cfg: object, name: str, default: int) -> int:
    try:
        return int(getattr(cfg, name, default))
    except (TypeError, ValueError):
        return default


async def fetch_bilibili_context(bvid: str, cfg: object) -> BilibiliContext:
    """获取视频元数据与热评，并按配置截断展示内容。"""
    view = BilibiliViewData()
    comments: list[BilibiliComment] = []
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(headers=COMMON_HEADERS, timeout=timeout) as session:
            async with session.get(
                "https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}
            ) as response:
                if response.status == 200:
                    view_payload = _read_api_data(await response.json())
                    if view_payload is not None:
                        view = BilibiliViewData.from_api_payload(view_payload)

            if view.aid is not None:
                async with session.get(
                    "https://api.bilibili.com/x/v2/reply/main",
                    params={"type": 1, "oid": view.aid, "mode": 3},
                ) as response:
                    if response.status == 200:
                        comment_payload = _read_api_data(await response.json()) or {}
                        replies = comment_payload.get("replies")
                        if isinstance(replies, list):
                            for reply in replies[: max(0, _config_value(cfg, "comment_count", 3))]:
                                if not isinstance(reply, dict):
                                    continue
                                content = reply.get("content")
                                message = content.get("message", "") if isinstance(content, dict) else ""
                                try:
                                    like = int(reply.get("like") or 0)
                                except (TypeError, ValueError):
                                    like = 0
                                comments.append(BilibiliComment(message=str(message), like=like))
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as exc:
        core.logger.error(f"[Bilibili] API 数据交互异常：{exc}")

    desc = view.desc
    desc_limit = max(0, _config_value(cfg, "desc_char_limit", 150))
    if len(desc) > desc_limit:
        desc = f"{desc[:desc_limit]}……"
    return BilibiliContext(
        title=view.title,
        uploader=view.uploader,
        desc=desc,
        duration=view.duration,
        comments=comments,
    )

