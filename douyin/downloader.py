"""抖音视频下载：无水印直链 → yt-dlp → 带水印/低码率 三级降级。

失败统一以 :class:`MediaDownloadError` 抛出（不再吞异常），由调用方转换为用户可见提示。
所有档位都先把产物写入临时位置，校验通过后才原子改名到 ``output_path``：直链档位写
``.part``，yt-dlp 档位写唯一临时目录 ``.ytdlp-<id>-<uuid>/``。因此成品文件永远只会是
被校验过的完整文件；任何异常、取消或超时都会清理临时文件/目录，绝不残留可能被误判为
缓存的半成品，也避免迟到线程覆盖后续同一作品的新下载结果。
同一 ``aweme_id`` 的并发请求复用同一后台任务，避免重复下载与文件竞态。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from uuid import uuid4

import aiohttp
from nekro_agent.api import core
from nekro_agent.api.plugin import dynamic_import_pkg

from ..download_kit import (
    FailureKind,
    InflightRegistry,
    MediaDownloadError,
    atomic_output,
    find_completed_media,
    part_path,
    probe_mp4,
    run_with_retry,
    translate_network_error,
)
from .api import COMMON_HEADERS

# yt-dlp 兜底时的画质选项。
QUALITY_FORMATS = {
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "540p": "bestvideo[height<=540]+bestaudio/best[height<=540]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "best": "bestvideo+bestaudio/best",
}

# 单块下载大小，边下边写，避免一次性读入内存。
_CHUNK_SIZE = 1 << 16
# 直链下载的连接/单次读取超时（总时长由外层统一控制）。
_CONNECT_TIMEOUT = 30
_READ_TIMEOUT = 30
# 重试策略：仅网络抖动/5xx，固定上限与退避，绝不无限重试。
_MAX_RETRIES = 2
_RETRY_BACKOFF = (1.0, 3.0)
# 整个下载（含所有降级档位）的默认总超时。
_DEFAULT_TOTAL_TIMEOUT = 180.0

_ACTIVE_TASKS: set[asyncio.Task[None]] = set()
_INFLIGHT = InflightRegistry()


def auto_clean_temp_dir(temp_dir: Path, max_age_hours: int = 24) -> None:
    if not temp_dir.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    try:
        for path in temp_dir.iterdir():
            if path.stat().st_mtime >= cutoff:
                continue
            if path.is_dir():
                # 仅清理下载专用的暂存目录，避免误删缓存目录中的其它内容。
                if path.name.startswith(".ytdlp-"):
                    shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink()
    except (OSError, ValueError) as exc:
        core.logger.warning(f"[Douyin] 缓存清理失败：{exc}")


def find_cached_file(temp_dir: Path, aweme_id: str) -> Path | None:
    """返回已完成的成品文件；``.part`` 等临时文件永不命中。"""
    return find_completed_media(temp_dir, aweme_id, ".mp4")


def _raise_for_status(status: int) -> None:
    """把直链响应状态码映射为带原因枚举的异常。"""
    if status in (401, 403):
        raise MediaDownloadError(FailureKind.AUTH, "抖音视频直链鉴权失效", detail=str(status))
    if status == 404:
        raise MediaDownloadError(FailureKind.UNAVAILABLE, "抖音视频地址不存在", detail=str(status))
    if status == 429:
        raise MediaDownloadError(FailureKind.NETWORK, "抖音请求过于频繁，请稍后重试", detail=str(status))
    if status >= 500:
        raise MediaDownloadError(FailureKind.SERVER, "抖音视频服务器错误", detail=str(status))
    if status >= 400:
        raise MediaDownloadError(FailureKind.UNAVAILABLE, "抖音视频不可访问", detail=str(status))


def _content_length(response: aiohttp.ClientResponse) -> int | None:
    raw = response.headers.get("Content-Length")
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def _stream_to_file(response: aiohttp.ClientResponse, temp_path: Path) -> int:
    written = 0
    with temp_path.open("wb") as handle:
        async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
            if not chunk:
                continue
            handle.write(chunk)
            written += len(chunk)
    return written


async def _download_direct(video_url: str, output_path: Path, referer: str) -> None:
    """通过直链流式下载：先写 ``.part``，校验通过后原子替换为成品。"""
    headers = {**COMMON_HEADERS, "Referer": referer}
    timeout = aiohttp.ClientTimeout(total=None, connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT)
    with atomic_output(output_path) as temp_path:
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(video_url) as response:
                    _raise_for_status(response.status)
                    expected = _content_length(response)
                    written = await _stream_to_file(response, temp_path)
        except MediaDownloadError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise translate_network_error(exc) from exc
        except OSError as exc:
            raise MediaDownloadError(FailureKind.LOCAL, "写入本地文件失败，请检查磁盘空间", detail=str(exc)) from exc
        if expected is not None and written != expected:
            raise MediaDownloadError(FailureKind.LOCAL, "下载数据不完整", detail=f"{written}/{expected}")
        if written <= 0:
            raise MediaDownloadError(FailureKind.LOCAL, "下载内容为空", detail=str(output_path))
        if not await asyncio.to_thread(probe_mp4, temp_path):
            raise MediaDownloadError(FailureKind.INVALID, "下载的视频校验未通过", detail=str(output_path))


def _download_with_ytdlp(aweme_id: str, out_tmpl: str, format_str: str, data_dir: Path) -> None:
    """yt-dlp 兜底下载（在线程中执行）。

    写入调用方给定的**唯一临时输出模板**；失败会抛异常，不再以 ``ignoreerrors`` 静默。
    真正的成品由调用方在“线程退出且校验通过”后原子发布，因此本函数绝不会直接写
    ``output_path``——即使线程被超时/取消后迟到，也只会在自己的临时目录内产生文件。
    """
    yt_dlp = dynamic_import_pkg("yt-dlp>=2025.1.15", import_name="yt_dlp")
    options = {
        "format": format_str,
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 10,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "http_headers": COMMON_HEADERS,
    }
    cookie_path = data_dir / "douyin_cookies.txt"
    if cookie_path.exists():
        options["cookiefile"] = str(cookie_path)
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([f"https://www.douyin.com/video/{aweme_id}"])


def _staging_dir(temp_dir: Path, aweme_id: str) -> Path:
    """为一次 yt-dlp 尝试分配唯一临时目录，隔离并发请求与迟到线程的写入。"""
    return temp_dir / f".ytdlp-{aweme_id}-{uuid4().hex}"


def _locate_staged_media(work_dir: Path) -> Path | None:
    """在临时目录中定位 yt-dlp 产出的成品，忽略 ``.part``/``.ytdl`` 等中间文件。

    正常情况下成功下载后目录内只剩最终媒体文件；若仍残留分片，取体积最大的
    ``mp4`` 文件，交由其后的校验决定是否可用。
    """
    if not work_dir.is_dir():
        return None
    files = [
        path
        for path in work_dir.iterdir()
        if path.is_file() and not path.name.endswith((".part", ".ytdl"))
    ]
    if not files:
        return None
    return max(files, key=lambda path: (path.suffix.lower() == ".mp4", path.stat().st_size))


def _verify_output(output_path: Path) -> None:
    """校验下载产物存在、非空、为有效 mp4；不通过即判失败。"""
    if not probe_mp4(output_path):
        raise MediaDownloadError(FailureKind.INVALID, "下载的视频校验未通过", detail=str(output_path))


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _retry_logger(aweme_id: str, tier: str):
    def _on_retry(exc: MediaDownloadError, attempt: int, delay: float) -> None:
        core.logger.warning(
            f"[Douyin] {tier}第 {attempt} 次重试（{delay:.0f}s 后）：{aweme_id} - {exc.detail}"
        )

    return _on_retry


async def _download(
    aweme_id: str,
    output_path: Path,
    video_url: str | None,
    watermark_url: str | None,
    quality: str,
    data_dir: Path,
) -> None:
    """按 无水印直链 → yt-dlp → 带水印/低码率 的顺序降级，全部失败则抛出异常。"""
    referer = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
    last_error: MediaDownloadError | None = None

    # 档位一：无水印直链（可重试）。
    if video_url:
        try:
            await run_with_retry(
                lambda: _download_direct(video_url, output_path, referer),
                max_retries=_MAX_RETRIES,
                backoff=_RETRY_BACKOFF,
                on_retry=_retry_logger(aweme_id, "无水印直链"),
            )
            core.logger.success(f"[Douyin] 无水印直链下载成功：{aweme_id}")
            return
        except MediaDownloadError as exc:
            if exc.kind is FailureKind.UNAVAILABLE:
                raise
            last_error = exc
            core.logger.warning(
                f"[Douyin] 无水印直链失败（{exc.kind.value}），降级 yt-dlp：{aweme_id} - {exc.detail}"
            )

    # 档位二：yt-dlp。先写入唯一临时目录，待线程退出且校验通过后再原子发布成品；
    # 任何失败/超时/取消都清理临时目录，绝不把中间产物留在 output_path 上——
    # 既避免残留半成品被当成缓存，也避免迟到线程覆盖后续同一作品的新下载结果。
    work_dir = _staging_dir(output_path.parent, aweme_id)
    try:
        await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
        format_str = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["720p"])
        await asyncio.to_thread(
            _download_with_ytdlp, aweme_id, str(work_dir / f"{aweme_id}.%(ext)s"), format_str, data_dir
        )
        staged = _locate_staged_media(work_dir)
        if staged is None:
            raise MediaDownloadError(FailureKind.INVALID, "下载的视频校验未通过", detail=str(work_dir))
        await asyncio.to_thread(_verify_output, staged)
        staged.replace(output_path)
        core.logger.success(f"[Douyin] yt-dlp 下载成功：{aweme_id}")
        return
    except MediaDownloadError as exc:
        last_error = exc
        core.logger.warning(f"[Douyin] yt-dlp 失败（{exc.kind.value}），继续降级：{aweme_id} - {exc.detail}")
    except Exception as exc:  # noqa: BLE001 - yt-dlp 的第三方异常需统一收口
        last_error = MediaDownloadError(FailureKind.LOCAL, "yt-dlp 下载失败", detail=str(exc))
        core.logger.warning(f"[Douyin] yt-dlp 失败，继续降级：{aweme_id} - {exc}")
    finally:
        # 同步清理，确保取消/超时路径（finally 中不能再 await）也能删除临时目录。
        shutil.rmtree(work_dir, ignore_errors=True)

    # 档位三：带水印 / 低码率地址。
    if watermark_url and watermark_url != video_url:
        try:
            await run_with_retry(
                lambda: _download_direct(watermark_url, output_path, referer),
                max_retries=_MAX_RETRIES,
                backoff=_RETRY_BACKOFF,
                on_retry=_retry_logger(aweme_id, "带水印兜底"),
            )
            core.logger.success(f"[Douyin] 带水印兜底下载成功：{aweme_id}")
            return
        except MediaDownloadError as exc:
            last_error = exc
            core.logger.warning(f"[Douyin] 带水印兜底失败（{exc.kind.value}）：{aweme_id} - {exc.detail}")

    if last_error is not None:
        raise last_error
    raise MediaDownloadError(FailureKind.PARSE, "该抖音作品没有可用的视频地址", detail=aweme_id)


async def _download_guarded(
    aweme_id: str,
    output_path: Path,
    video_url: str | None,
    watermark_url: str | None,
    quality: str,
    data_dir: Path,
    total_timeout: float,
) -> None:
    """为整个下载流程施加总超时，超时后清理临时文件并抛出 TIMEOUT。"""
    try:
        await asyncio.wait_for(
            _download(aweme_id, output_path, video_url, watermark_url, quality, data_dir),
            timeout=total_timeout,
        )
    except TimeoutError as exc:
        _discard(part_path(output_path))
        raise MediaDownloadError(
            FailureKind.TIMEOUT, "下载超时，请稍后重试", detail=f"{total_timeout:.0f}s"
        ) from exc


def schedule_download(
    aweme_id: str,
    output_path: Path,
    video_url: str | None,
    quality: str,
    data_dir: Path,
    *,
    watermark_url: str | None = None,
    timeout: float = _DEFAULT_TOTAL_TIMEOUT,
) -> asyncio.Task[None]:
    """调度后台下载；同一 ``aweme_id`` 进行中时复用同一任务，避免重复下载。"""
    existing = _INFLIGHT.get(aweme_id)
    if existing is not None:
        return existing

    task = asyncio.create_task(
        _download_guarded(aweme_id, output_path, video_url, watermark_url, quality, data_dir, timeout),
        name=f"douyin-{aweme_id}",
    )
    _INFLIGHT.register(aweme_id, task)
    _ACTIVE_TASKS.add(task)
    task.add_done_callback(_ACTIVE_TASKS.discard)
    return task


async def cancel_download_tasks() -> None:
    tasks = set(_ACTIVE_TASKS) | set(_INFLIGHT.tasks())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _ACTIVE_TASKS.clear()
    _INFLIGHT.clear()
