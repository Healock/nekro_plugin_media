"""抖音视频下载：无水印直链为主，yt-dlp 兜底。

下载在后台协程中进行，视频段落会直接指向尚未落地的目标路径；
上层媒体流程会轮询等待文件出现（与 Bilibili 子模块一致）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiohttp
from nekro_agent.api import core
from nekro_agent.api.plugin import dynamic_import_pkg

from .api import COMMON_HEADERS

# yt-dlp 兜底时的画质选项。
QUALITY_FORMATS = {
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "540p": "bestvideo[height<=540]+bestaudio/best[height<=540]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "best": "bestvideo+bestaudio/best",
}

_ACTIVE_TASKS: set[asyncio.Task[None]] = set()
# 单块下载大小，边下边写，避免一次性读入内存。
_CHUNK_SIZE = 1 << 16


def auto_clean_temp_dir(temp_dir: Path, max_age_hours: int = 24) -> None:
    if not temp_dir.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    try:
        for path in temp_dir.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
    except (OSError, ValueError) as exc:
        core.logger.warning(f"[Douyin] 缓存清理失败：{exc}")


def find_cached_file(temp_dir: Path, aweme_id: str) -> Path | None:
    for path in temp_dir.glob(f"{aweme_id}.*"):
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


async def _download_direct(video_url: str, output_path: Path, referer: str) -> None:
    """通过无水印直链流式下载视频到 output_path。"""
    headers = {**COMMON_HEADERS, "Referer": referer}
    timeout = aiohttp.ClientTimeout(total=None, sock_read=30)
    temp_path = output_path.with_name(f"{output_path.name}.part")
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(video_url) as response:
            response.raise_for_status()
            # 分块落盘：单块 64KiB，写入耗时极短，可接受在事件循环中同步写。
            with open(temp_path, "wb") as handle:  # noqa: ASYNC230
                async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                    handle.write(chunk)
    temp_path.replace(output_path)


def _download_with_ytdlp(aweme_id: str, output_path: str, format_str: str, data_dir: Path) -> None:
    yt_dlp = dynamic_import_pkg("yt-dlp>=2025.1.15", import_name="yt_dlp")
    options = {
        "format": format_str,
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 10,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "http_headers": COMMON_HEADERS,
    }
    cookie_path = data_dir / "douyin_cookies.txt"
    if cookie_path.exists():
        options["cookiefile"] = str(cookie_path)
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([f"https://www.douyin.com/video/{aweme_id}"])


async def _download(
    aweme_id: str,
    output_path: Path,
    video_url: str | None,
    quality: str,
    data_dir: Path,
) -> None:
    referer = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    if video_url:
        try:
            await _download_direct(video_url, output_path, referer)
            if output_path.exists() and output_path.stat().st_size > 0:
                return
            core.logger.warning(f"[Douyin] 直链下载未产生文件，回退 yt-dlp：{aweme_id}")
        except Exception as exc:  # noqa: BLE001 - 兜底链路需吞掉所有异常
            core.logger.warning(f"[Douyin] 直链下载失败，回退 yt-dlp：{exc}")

    try:
        format_str = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["720p"])
        await asyncio.to_thread(_download_with_ytdlp, aweme_id, str(output_path), format_str, data_dir)
    except Exception as exc:  # noqa: BLE001
        core.logger.error(f"[Douyin] yt-dlp 下载失败（{aweme_id}）：{exc}")


def schedule_download(
    aweme_id: str,
    output_path: Path,
    video_url: str | None,
    quality: str,
    data_dir: Path,
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        _download(aweme_id, output_path, video_url, quality, data_dir),
        name=f"douyin-{aweme_id}",
    )
    _ACTIVE_TASKS.add(task)
    task.add_done_callback(_ACTIVE_TASKS.discard)
    return task


async def cancel_download_tasks() -> None:
    tasks = tuple(_ACTIVE_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _ACTIVE_TASKS.clear()
