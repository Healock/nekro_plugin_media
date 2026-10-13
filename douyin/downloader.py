"""抖音视频下载：无水印直链 → yt-dlp → 带水印/低码率 三级降级。

失败统一以 :class:`MediaDownloadError` 抛出（不再吞异常），由调用方转换为用户可见提示。
所有档位都先把产物写入临时位置，校验通过后才原子改名到 ``output_path``：直链档位写
``.part``，yt-dlp 档位写唯一暂存目录 ``.ytdlp-<id>-<uuid>/``。因此成品文件永远只会是
被校验过的完整文件；任何异常、取消或超时都会清理临时文件/目录，绝不残留可能被误判为
缓存的半成品，也避免迟到线程覆盖后续同一作品的新下载结果。

yt-dlp 档位的暂存目录生命周期由共享执行器 :func:`download_kit.run_staged_download` 在
**同一个工作线程**内顺序收尾（建目录 → 下载 → 校验 → 原子发布 → 清理），因此取消方绝不
与 worker 并发清理；“清理与 worker 并发”“目录被删后 worker 重新落盘”均不可能发生。

超时/取消通过 ``threading.Event`` 协作式发信号（yt-dlp 进度钩子据此尽快中止下载），并
短路后续档位；由 :class:`download_kit.DownloadManager` 收尾——``cancel_all`` 会先置位取消
信号，再在有界时间内**等待 worker 真正退出**（``asyncio.to_thread`` 的等待只在线程退出时才
resolve），到点才释放登记，避免“旧 worker 未退 + 同 id 新任务”并存的重复下载。

同一 ``aweme_id`` 的并发请求复用同一后台任务，避免重复下载与文件竞态。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable
from pathlib import Path

import aiohttp
from nekro_agent.api import core
from nekro_agent.api.plugin import dynamic_import_pkg

from ..download_kit import (
    DownloadCancelled,
    DownloadManager,
    FailureKind,
    MediaDownloadError,
    atomic_output,
    clean_stale_staging,
    ensure_playable_mp4,
    find_completed_media,
    part_path,
    probe_mp4,
    run_staged_download,
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
# 取消时等待 yt-dlp worker 线程退出的上限（线程无法强杀，超出后强制取消协程）。
_CANCEL_GRACE = 5.0

# 按 aweme_id 去重的下载登记表：并发复用同一任务，并承担超时/取消后的有界等待收尾。
_DOWNLOADS = DownloadManager("douyin")


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


async def _download_direct(
    video_url: str, output_path: Path, referer: str, cookie: str | None = None
) -> None:
    """通过直链流式下载：先写 ``.part``，校验通过后原子替换为成品。"""
    headers = {**COMMON_HEADERS, "Referer": referer}
    if cookie:
        headers["Cookie"] = cookie
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


def _download_with_ytdlp(
    aweme_id: str,
    out_tmpl: str,
    format_str: str,
    data_dir: Path,
    cancel_event: threading.Event,
    cookie: str | None = None,
) -> None:
    """yt-dlp 兜底下载（在工作线程内执行，产出写入调用方给定的唯一暂存模板）。

    失败会抛异常，不再以 ``ignoreerrors`` 静默。真正的成品由共享执行器在“线程退出且校验
    通过”后原子发布，因此本函数绝不会直接写 ``output_path``——即使线程被超时/取消后迟到，
    也只会在自己的暂存目录内产生文件。

    ``cancel_event`` 为协作式取消信号：注册进度钩子，在被置位时抛 :class:`DownloadCancelled`
    尽快中止下载（线程无法被强制终止，这是“不引子进程”约束下可用的最快收敛手段）。
    """
    yt_dlp = dynamic_import_pkg("yt-dlp>=2025.1.15", import_name="yt_dlp")

    def _abort_if_cancelled(_status: dict[str, object]) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled(f"cancel requested for {aweme_id}")

    headers = dict(COMMON_HEADERS)
    cookie_path = data_dir / "douyin_cookies.txt"
    if not cookie_path.exists() and cookie:
        headers["Cookie"] = cookie

    options = {
        "format": format_str,
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 10,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "http_headers": headers,
        "progress_hooks": [_abort_if_cancelled],
    }
    if cookie_path.exists():
        options["cookiefile"] = str(cookie_path)
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([f"https://www.douyin.com/video/{aweme_id}"])


def _verify_output(output_path: Path) -> None:
    """校验下载产物存在、非空、为有效 mp4；不通过即判失败。"""
    ensure_playable_mp4(output_path)


def _download_ytdlp_staged(
    aweme_id: str,
    output_path: Path,
    format_str: str,
    data_dir: Path,
    cancel_event: threading.Event,
    cookie: str | None = None,
) -> bool:
    """在工作线程内，经共享执行器自持 yt-dlp 暂存目录的整个生命周期。

    建目录 → 下载 → 定位产物 → 校验 → 原子发布 → 清理由
    :func:`download_kit.run_staged_download` 在**同一线程内顺序完成**，因此取消/超时后取消方
    不会再并发删除目录，也不存在“目录被删后 worker 重新落盘”。取消经 ``cancel_event`` 协作式
    触发：下载中被进度钩子尽快中止，发布前若已取消则丢弃暂存产物（保持“取消即无副作用”）。
    返回是否真正发布了成品。
    """

    def produce(work_dir: Path, event: threading.Event) -> None:
        args = (aweme_id, str(work_dir / f"{aweme_id}.%(ext)s"), format_str, data_dir, event)
        if cookie is None:
            _download_with_ytdlp(*args)
        else:
            _download_with_ytdlp(*args, cookie)

    try:
        return run_staged_download(
            output_path.parent, output_path, aweme_id, produce, cancel_event, verify=_verify_output
        )
    except DownloadCancelled:
        # 协作式取消在中途生效：丢弃暂存产物、不发布，等同于“取消即无副作用”。
        core.logger.warning(f"[Douyin] yt-dlp 下载已取消：{aweme_id}")
        return False


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
    cancel_event: threading.Event,
    cookie: str | None = None,
) -> None:
    """按 无水印直链 → yt-dlp → 带水印/低码率 的顺序降级，全部失败则抛出异常。

    取消信号 ``cancel_event`` 置位后：不再发起新的尝试，也不再进入后续档位（短路），
    已开始的 yt-dlp 下载由进度钩子尽快中止、暂存产物交由执行器丢弃。
    """
    referer = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
    last_error: MediaDownloadError | None = None

    def download_direct(url: str) -> Awaitable[None]:
        if cookie is None:
            return _download_direct(url, output_path, referer)
        return _download_direct(url, output_path, referer, cookie)

    # 档位一：无水印直链（可重试）。取消即短路后续尝试与档位。
    if video_url and not cancel_event.is_set():
        try:
            await run_with_retry(
                lambda: download_direct(video_url),
                max_retries=_MAX_RETRIES,
                backoff=_RETRY_BACKOFF,
                on_retry=_retry_logger(aweme_id, "无水印直链"),
                should_stop=cancel_event.is_set,
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

    if cancel_event.is_set():
        return

    # 档位二：yt-dlp。暂存目录整个生命周期收拢在同一工作线程内（共享执行器），只有 worker
    # 结束后才删目录，取消方不再并发清理；取消时返回未发布，成品路径不会写入。
    format_str = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["720p"])
    try:
        published = await asyncio.to_thread(
            _download_ytdlp_staged,
            aweme_id,
            output_path,
            format_str,
            data_dir,
            cancel_event,
            cookie,
        )
    except MediaDownloadError as exc:
        last_error = exc
        core.logger.warning(f"[Douyin] yt-dlp 失败（{exc.kind.value}），继续降级：{aweme_id} - {exc.detail}")
    except Exception as exc:  # noqa: BLE001 - yt-dlp 的第三方异常需统一收口
        last_error = MediaDownloadError(FailureKind.LOCAL, "yt-dlp 下载失败", detail=str(exc))
        core.logger.warning(f"[Douyin] yt-dlp 失败，继续降级：{aweme_id} - {exc}")
    else:
        if published:
            core.logger.success(f"[Douyin] yt-dlp 下载成功：{aweme_id}")
            return

    if cancel_event.is_set():
        core.logger.warning(f"[Douyin] 已取消，短路后续档位：{aweme_id}")
        return

    # 档位三：带水印 / 低码率地址。
    if watermark_url and watermark_url != video_url:
        try:
            await run_with_retry(
                lambda: download_direct(watermark_url),
                max_retries=_MAX_RETRIES,
                backoff=_RETRY_BACKOFF,
                on_retry=_retry_logger(aweme_id, "带水印兜底"),
                should_stop=cancel_event.is_set,
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
    cancel_event: threading.Event,
    cookie: str | None = None,
) -> None:
    """为整个下载流程施加总超时；超时或外层取消时置位协作式取消信号并清理 ``.part``。

    置位 ``cancel_event`` 是超时/取消路径下让仍在运行的 yt-dlp worker **尽快收敛**的关键：
    ``to_thread`` 的线程无法强杀，只能靠协作信号让它在下次进度回调处自行退出。
    """
    try:
        await asyncio.wait_for(
            _download(
                aweme_id,
                output_path,
                video_url,
                watermark_url,
                quality,
                data_dir,
                cancel_event,
                cookie,
            ),
            timeout=total_timeout,
        )
    except TimeoutError as exc:
        cancel_event.set()
        _discard(part_path(output_path))
        raise MediaDownloadError(
            FailureKind.TIMEOUT, "下载超时，请稍后重试", detail=f"{total_timeout:.0f}s"
        ) from exc
    except asyncio.CancelledError:
        # 外层取消（task.cancel 等）：同样协作式收敛 worker，再向上传播取消。
        cancel_event.set()
        raise


def schedule_download(
    aweme_id: str,
    output_path: Path,
    video_url: str | None,
    quality: str,
    data_dir: Path,
    *,
    watermark_url: str | None = None,
    timeout: float = _DEFAULT_TOTAL_TIMEOUT,
    cookie: str | None = None,
) -> asyncio.Task[None]:
    """调度后台下载；同一 ``aweme_id`` 进行中时复用同一任务，避免重复下载。"""

    def factory(cancel_event: threading.Event) -> Awaitable[None]:
        return _download_guarded(
            aweme_id,
            output_path,
            video_url,
            watermark_url,
            quality,
            data_dir,
            timeout,
            cancel_event,
            cookie,
        )

    return _DOWNLOADS.schedule(aweme_id, factory)


async def cancel_download_tasks() -> None:
    """协作式取消全部下载，并在有界时间内等待 worker 退出后再释放登记。

    不再“取消协程后立即返回并清空登记”：先置位取消信号，再等待 worker 线程真正退出
    （或到达 ``_CANCEL_GRACE`` 上限），随后才释放登记，避免“旧 worker 未退 + 同 id 新任务”
    并存的重复下载。
    """
    await _DOWNLOADS.cancel_all(grace=_CANCEL_GRACE)
