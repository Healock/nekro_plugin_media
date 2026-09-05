from __future__ import annotations

import asyncio
import time
from pathlib import Path

from nekro_agent.api import core
from nekro_agent.api.plugin import dynamic_import_pkg

from .api import COMMON_HEADERS

QUALITY_FORMATS = {
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "best": "bestvideo+bestaudio/best",
}
_ACTIVE_TASKS: set[asyncio.Task[None]] = set()


def auto_clean_temp_dir(temp_dir: Path, max_age_hours: int = 24) -> None:
    if not temp_dir.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    try:
        for path in temp_dir.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
    except (OSError, ValueError) as exc:
        core.logger.warning(f"[Bilibili] 缓存清理失败：{exc}")


def find_cached_file(temp_dir: Path, bvid: str) -> Path | None:
    for path in temp_dir.glob(f"{bvid}.*"):
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def background_download_task(bvid: str, output_path: str, format_str: str, data_dir: Path) -> None:
    try:
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
        cookie_path = data_dir / "bilibili_cookies.txt"
        if cookie_path.exists():
            options["cookiefile"] = str(cookie_path)
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([f"https://www.bilibili.com/video/{bvid}"])
    except Exception as exc:
        core.logger.error(f"[Bilibili] 下载失败（{bvid}）：{exc}")


def schedule_download(bvid: str, output_path: Path, quality: str, data_dir: Path) -> asyncio.Task[None]:
    format_str = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["480p"])
    task = asyncio.create_task(
        asyncio.to_thread(background_download_task, bvid, str(output_path), format_str, data_dir),
        name=f"bilibili-{bvid}",
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
