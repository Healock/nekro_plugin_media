"""Bilibili 视频下载：yt-dlp 下载 → 校验 → 原子发布，复用共享的下载执行器。

与抖音链路一致地收敛失败处理：yt-dlp 写入**唯一暂存目录**（``.ytdlp-<bvid>-<uuid>/``），
由共享执行器 :func:`download_kit.run_staged_download` 在**同一个工作线程**内完成「下载 →
校验 → 原子改名发布 → 清理」。因此：

- ``ignoreerrors`` 关闭：下载失败会**抛出**而非静默，交由执行器清理暂存目录；
- 失败/中断**绝不**在成品路径 ``<bvid>.mp4`` 留下半成品——只有经校验通过的完整文件才会被
  原子改名到成品路径，``find_completed_media`` 不会再把它误判为缓存命中；
- 超时/取消经 ``threading.Event`` 协作式发信号，由 :class:`download_kit.DownloadManager`
  在有界时间内等待 worker 退出后再释放登记，避免同 id 重复下载。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable
from pathlib import Path

from nekro_agent.api import core
from nekro_agent.api.plugin import dynamic_import_pkg

from ..download_kit import (
    DownloadCancelled,
    DownloadManager,
    clean_stale_staging,
    ensure_playable_mp4,
    find_completed_media,
    run_staged_download,
)
from .api import COMMON_HEADERS

QUALITY_FORMATS = {
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "best": "bestvideo+bestaudio/best",
}
# 取消时等待 yt-dlp worker 线程退出的上限（线程无法强杀，超出后强制取消协程）。
_CANCEL_GRACE = 5.0

_DOWNLOADS = DownloadManager("bilibili")


def auto_clean_temp_dir(temp_dir: Path, max_age_hours: int = 24) -> None:
    if not temp_dir.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    try:
        clean_stale_staging(temp_dir, cutoff)
        for path in temp_dir.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
    except (OSError, ValueError) as exc:
        core.logger.warning(f"[Bilibili] 缓存清理失败：{exc}")


def find_cached_file(temp_dir: Path, bvid: str) -> Path | None:
    # 复用共享工具：只有精确命名的完成态成品文件才算命中，`.part` 等临时文件永不返回。
    return find_completed_media(temp_dir, bvid, ".mp4")


def _download_with_ytdlp(
    bvid: str, out_tmpl: str, format_str: str, data_dir: Path, cancel_event: threading.Event
) -> None:
    """yt-dlp 下载（在工作线程内执行，产出写入调用方给定的唯一暂存模板）。

    关闭 ``ignoreerrors``：失败会**抛出**，交由共享执行器清理暂存目录，绝不在成品路径
    留下半成品。``cancel_event`` 置位时经进度钩子抛 :class:`DownloadCancelled` 尽快中止。
    """
    yt_dlp = dynamic_import_pkg("yt-dlp>=2025.1.15", import_name="yt_dlp")

    def _abort_if_cancelled(_status: dict[str, object]) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled(f"cancel requested for {bvid}")

    options = {
        "format": format_str,
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 10,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "http_headers": COMMON_HEADERS,
        "progress_hooks": [_abort_if_cancelled],
    }
    cookie_path = data_dir / "bilibili_cookies.txt"
    if cookie_path.exists():
        options["cookiefile"] = str(cookie_path)
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([f"https://www.bilibili.com/video/{bvid}"])


def background_download_task(
    bvid: str, output_path: Path, format_str: str, data_dir: Path, cancel_event: threading.Event
) -> bool:
    """在工作线程内自持 yt-dlp 暂存目录的整个生命周期（建目录 → 下载 → 校验 → 发布 → 清理）。

    复用共享执行器 :func:`download_kit.run_staged_download`：失败抛异常、暂存目录被清理、
    成品路径不变；只有校验通过的完整文件才会被原子发布。返回是否真正发布了成品。
    """

    def produce(work_dir: Path, event: threading.Event) -> None:
        _download_with_ytdlp(bvid, str(work_dir / f"{bvid}.%(ext)s"), format_str, data_dir, event)

    try:
        return run_staged_download(
            output_path.parent, output_path, bvid, produce, cancel_event, verify=ensure_playable_mp4
        )
    except DownloadCancelled:
        # 协作式取消在中途生效：丢弃暂存产物、不发布，等同于“取消即无副作用”。
        core.logger.warning(f"[Bilibili] 下载已取消：{bvid}")
        return False


def schedule_download(bvid: str, output_path: Path, quality: str, data_dir: Path) -> asyncio.Task[None]:
    format_str = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["480p"])

    def factory(cancel_event: threading.Event) -> Awaitable[None]:
        return asyncio.to_thread(background_download_task, bvid, output_path, format_str, data_dir, cancel_event)

    return _DOWNLOADS.schedule(bvid, factory)


async def cancel_download_tasks() -> None:
    """协作式取消全部下载，并在有界时间内等待 worker 退出后再释放登记。"""
    await _DOWNLOADS.cancel_all(grace=_CANCEL_GRACE)
