"""抖音数据模型与纯解析逻辑。

本模块刻意只依赖 `pydantic` 与标准库，不引入网络请求或机器人框架，
以便核心解析逻辑（分享页 `_ROUTER_DATA` 提取、去水印地址转换、字段映射）
可以被独立单元测试覆盖。
"""

from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

# 可被消息正文命中的抖音链接形态（短链 / 网页长链 / 分享页 / m 站）。
DOUYIN_URL_REGEX = (
    r"(?:v\.douyin\.com/[A-Za-z0-9_\-]+"
    r"|jx\.douyin\.com/[A-Za-z0-9_\-]+"
    r"|(?:www\.)?douyin\.com/(?:video|note|slides)/\d+"
    r"|iesdouyin\.com/share/(?:video|note|slides)/\d+"
    r"|m\.douyin\.com/share/(?:video|note|slides)/\d+)"
)

# 抖音作品 ID（aweme_id）为 19 位左右数字，前后不允许再跟数字。
_AWEME_ID_REGEX = re.compile(r"(?<!\d)(\d{15,21})(?!\d)")

# 分享页把结构化数据内联在 `window._ROUTER_DATA = {...}` 里。
_ROUTER_MARKER = "window._ROUTER_DATA"

# 官方接口域名，用于在缺少 url_list 时按 uri 兜底拼装播放地址。
_PLAY_ENDPOINT = "https://aweme.snssdk.com/aweme/v1/play/"


def normalize_play_url(url: str) -> str:
    """把带水印的播放地址转换为无水印直链。

    分享页 `play_addr` 默认返回 `.../playwm/...`，将 `playwm` 替换为 `play`
    即可拿到无水印的视频流地址。
    """
    return url.replace("playwm", "play")


def build_play_url_from_uri(uri: str, ratio: str = "720p") -> str:
    """根据播放 uri 拼装官方播放端点（无签名、无 Cookie）。"""
    return f"{_PLAY_ENDPOINT}?video_id={uri}&ratio={ratio}"


def extract_aweme_id(text: str) -> str | None:
    """从 URL 或重定向 Location 中提取作品 ID。"""
    match = _AWEME_ID_REGEX.search(text)
    return match.group(1) if match else None


def extract_router_data(html: str) -> dict | None:
    """从分享页 HTML 中提取并解析 `window._ROUTER_DATA`。

    Returns:
        解析成功返回 dict，未找到或解析失败返回 None。
    """
    marker_index = html.find(_ROUTER_MARKER)
    if marker_index == -1:
        return None
    brace_start = html.find("{", marker_index)
    if brace_start == -1:
        return None
    raw = _match_braces(html, brace_start)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _match_braces(text: str, start: int) -> str | None:
    """在 text[start] 为 '{' 时，返回配平到首个顶层 '}' 的子串。

    正确处理 JSON 字符串内的引号与反斜杠转义，避免被字符串中的花括号干扰。
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if in_string and char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def find_item(router_data: dict) -> dict | None:
    """在 `_ROUTER_DATA` 中定位作品条目（`videoInfoRes.item_list[0]`）。"""
    loader = router_data.get("loaderData")
    if not isinstance(loader, dict):
        return None
    for entry in loader.values():
        item = _find_item_in(entry)
        if item is not None:
            return item
    return None


def _find_item_in(node: object) -> dict | None:
    if not isinstance(node, dict):
        return None
    video_info = node.get("videoInfoRes")
    if isinstance(video_info, dict):
        item_list = video_info.get("item_list")
        if isinstance(item_list, list) and item_list and isinstance(item_list[0], dict):
            return item_list[0]
    for value in node.values():
        found = _find_item_in(value)
        if found is not None:
            return found
    return None


def _first_url(container: object) -> str | None:
    if not isinstance(container, dict):
        return None
    urls = container.get("url_list")
    if not isinstance(urls, list):
        return None
    for candidate in urls:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def resolve_play_url(play_addr: object) -> str | None:
    """从 `play_addr` 中解析出无水印播放地址。"""
    if not isinstance(play_addr, dict):
        return None
    url = _first_url(play_addr)
    if url:
        return normalize_play_url(url)
    uri = play_addr.get("uri")
    if isinstance(uri, str) and uri:
        if uri.startswith(("http://", "https://")):
            return normalize_play_url(uri)
        return build_play_url_from_uri(uri)
    return None


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_seconds(value: object) -> int:
    """抖音 `duration` 单位为毫秒，转换为秒。"""
    milliseconds = _to_int(value)
    return round(milliseconds / 1000) if milliseconds > 0 else 0


def truncate_text(text: str, limit: int) -> str:
    if limit > 0 and len(text) > limit:
        return f"{text[:limit]}……"
    return text


def is_safe_media_url(value: object) -> bool:
    """仅接受可供媒体客户端访问的公开 HTTP(S) 地址。

    DTK 返回的地址来自插件配置的外部服务，不能默认继承内置抖音解析的信任边界。
    这里拒绝本地/保留 IP、localhost 和本地域名；域名解析后的访问仍由部署网络策略控制。
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username or parsed.password:
        return False
    host = host.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


class DouyinContext(BaseModel):
    """从分享页解析出的抖音作品上下文。"""

    desc: str = ""
    author: str = "未知作者"
    avatar_url: str | None = None
    duration: int = 0
    create_time: int = 0
    video_url: str | None = None
    # 带水印 / 低码率的原始播放地址，用作直链与 yt-dlp 均失败后的最后降级档位。
    video_url_watermarked: str | None = None
    # 仅供本次下载请求使用，不参与序列化或提示词，避免匿名 Cookie 泄漏到日志/历史。
    download_cookie: str | None = Field(default=None, exclude=True, repr=False)
    cover_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    digg_count: int = 0
    comment_count: int = 0

    @property
    def is_gallery(self) -> bool:
        """图文帖（笔记）判定：存在图片即视为图集。"""
        return bool(self.image_urls) and not bool(self.video_url)

    def title(self, limit: int = 150) -> str:
        return truncate_text(self.desc.strip() or "无标题", limit)

    def format_stats(self) -> str:
        return f"点赞 {self.digg_count} · 评论 {self.comment_count}"

    @classmethod
    def from_dtk_data(cls, data: dict) -> "DouyinContext":
        """映射 DTK API 的公开字段，不保留其额外响应数据。"""
        media = data.get("media") if isinstance(data.get("media"), dict) else {}
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        images_raw = media.get("images")
        image_urls: list[str] = []
        if isinstance(images_raw, list):
            for image in images_raw:
                if is_safe_media_url(image):
                    image_urls.append(image)
                elif isinstance(image, dict):
                    urls = image.get("urls")
                    candidate = image.get("url")
                    if not candidate and isinstance(urls, list):
                        candidate = next((url for url in urls if isinstance(url, str) and url), None)
                    if is_safe_media_url(candidate):
                        image_urls.append(candidate)
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        author = data.get("author")
        if isinstance(author, dict):
            author = author.get("nickname") or author.get("name")
        video_urls = video.get("urls")
        video_url = video.get("url")
        if not is_safe_media_url(video_url):
            video_url = None
        if not video_url and isinstance(video_urls, list):
            video_url = next((url for url in video_urls if is_safe_media_url(url)), None)
        return cls(
            desc=str(data.get("description") or data.get("title") or ""),
            author=str(author or "未知作者"),
            duration=_to_seconds(data.get("duration_ms")),
            video_url=str(video_url) if video_url else None,
            image_urls=image_urls,
            digg_count=_to_int(stats.get("digg_count")),
            comment_count=_to_int(stats.get("comment_count")),
        )

    @classmethod
    def from_media_parser_data(cls, data: dict) -> "DouyinContext":
        """Map ucmao/media-parser's flat response contract."""
        image_urls: list[str] = []
        raw_images = data.get("image_list")
        if isinstance(raw_images, list):
            for image in raw_images:
                candidate = image.get("url") if isinstance(image, dict) else image
                if is_safe_media_url(candidate):
                    image_urls.append(candidate)

        author = data.get("author")
        if isinstance(author, dict):
            author = author.get("nickname") or author.get("name")
        video_url = data.get("video_url") or data.get("source_video_url")
        if not is_safe_media_url(video_url):
            video_url = None
        video_list = data.get("video_list")
        if not video_url and isinstance(video_list, list):
            video_url = next((url for url in video_list if is_safe_media_url(url)), None)
        duration = (
            _to_seconds(data.get("duration_ms"))
            if data.get("duration_ms") is not None
            else _to_int(data.get("duration"))
        )
        return cls(
            desc=str(data.get("desc") or data.get("title") or ""),
            author=str(author or "未知作者"),
            duration=duration,
            video_url=str(video_url) if video_url else None,
            image_urls=image_urls,
            cover_url=data.get("cover_url") if is_safe_media_url(data.get("cover_url")) else None,
        )

    @classmethod
    def from_router_data(cls, router_data: dict) -> "DouyinContext | None":
        item = find_item(router_data)
        if item is None:
            return None
        return cls.from_item(item)

    @classmethod
    def from_item(cls, item: dict) -> "DouyinContext":
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        video = item.get("video") if isinstance(item.get("video"), dict) else None
        statistics = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}

        image_urls: list[str] = []
        images = item.get("images")
        if isinstance(images, list):
            for image in images:
                url = _first_url(image)
                if url:
                    image_urls.append(url)

        video_url = None
        video_url_watermarked = None
        cover_url = None
        duration = 0
        if video:
            play_addr = video.get("play_addr")
            video_url_watermarked = _first_url(play_addr)
            video_url = resolve_play_url(play_addr)
            cover_url = _first_url(video.get("cover"))
            duration = _to_seconds(video.get("duration"))

        return cls(
            desc=str(item.get("desc") or ""),
            author=str(author.get("nickname") or "未知作者"),
            avatar_url=_first_url(author.get("avatar_thumb"))
            or _first_url(author.get("avatar_medium")),
            duration=duration,
            create_time=_to_int(item.get("create_time")),
            video_url=video_url,
            video_url_watermarked=video_url_watermarked,
            cover_url=cover_url,
            image_urls=image_urls,
            digg_count=_to_int(statistics.get("digg_count")),
            comment_count=_to_int(statistics.get("comment_count")),
        )
