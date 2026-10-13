"""通用媒体下载工具：完成态判定、原子落盘、并发去重、有限重试与统一异常。

本模块刻意只依赖标准库（`asyncio` / `pathlib` / `subprocess` 等），不引入
`aiohttp`、`nekro_agent` 或机器人框架，以便“失败处理”这类核心逻辑能被独立单元测试覆盖，
并可同时被 Bilibili、抖音等子模块复用（避免每个解析器各修一套、把同样的坑换个地方）。

设计要点：
- **完成态判定**：只有落盘的成品文件（文件名精确匹配且含 `ftyp` box）才算命中，
  `.part` 及任何临时/分片文件永不作为缓存结果返回。
- **原子落盘**：先写 `<name>.part`，校验通过后再 `rename` 成最终文件；任何异常、
  取消或超时都会清理临时文件，绝不残留半截文件。
- **暂存执行器**：`run_staged_download` 把「唯一暂存目录 → 下载 → 校验 → 原子发布 →
  清理」收拢进**同一个线程**内顺序完成，供 yt-dlp 之类的生产者复用；`DownloadManager`
  负责按 key 去重调度与「协作式取消 + 有界等待 worker 退出」的收尾。
- **统一异常**：`MediaDownloadError` + `FailureKind` 枚举，区分可重试、鉴权、永久失败、
  本地失败与产物无效，供上层生成面向用户的提示。
- **有限重试**：仅对可重试原因做固定次数、固定退避的重试，绝不无限重试。
"""

from __future__ import annotations

import asyncio
import enum
import shutil
import subprocess
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

__all__ = [
    "STAGING_PREFIX",
    "DownloadCancelled",
    "DownloadManager",
    "FailureKind",
    "InflightRegistry",
    "MediaDownloadError",
    "atomic_output",
    "clean_stale_staging",
    "ensure_complete_mp4",
    "ensure_playable_mp4",
    "find_completed_media",
    "is_complete_mp4",
    "locate_staged_media",
    "part_path",
    "probe_mp4",
    "run_staged_download",
    "run_with_retry",
    "staging_dir",
    "translate_network_error",
]

# MP4 文件头中的 'ftyp' box 类型标识；正常 mp4 的前若干字节内必然出现。
_FTYP = b"ftyp"
# 临时文件后缀：只有去掉该后缀的成品文件才对上层可见。
_PART_SUFFIX = ".part"
# 头部探测读取的字节数，足以覆盖 ftyp box（通常位于偏移 4）。
_HEAD_PROBE_BYTES = 64
# 下载专用暂存目录前缀：只有成品文件对上层可见，暂存目录永不被当作缓存命中。
STAGING_PREFIX = ".ytdlp-"


class FailureKind(str, enum.Enum):
    """下载/解析失败原因，决定是否可重试以及面向用户的提示语。"""

    NETWORK = "network"          # 可重试：连接失败/重置等网络抖动
    SERVER = "server"            # 可重试：服务端 5xx
    TIMEOUT = "timeout"          # 可重试：读写或总时长超时
    AUTH = "auth"                # 鉴权：403/401/空 body 等，刷新凭证后仅重试一次
    UNAVAILABLE = "unavailable"  # 永久失败：作品不存在/已删除/私密/地域限制/需登录
    PARSE = "parse"              # 永久失败：页面结构变化或触发风控，无法解析
    LOCAL = "local"              # 本地失败：磁盘满/路径不可写/数据不完整
    INVALID = "invalid"          # 产物无效：下载完成但文件损坏

    @property
    def retryable(self) -> bool:
        """是否为可重试原因（网络抖动 / 5xx / 超时）。"""
        return self in (FailureKind.NETWORK, FailureKind.SERVER, FailureKind.TIMEOUT)


class MediaDownloadError(Exception):
    """下载链路统一异常，携带失败原因枚举与面向用户的可读描述。"""

    def __init__(self, kind: FailureKind, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail or message

    @property
    def retryable(self) -> bool:
        return self.kind.retryable

    def user_message(self) -> str:
        """返回可直接展示给用户的提示语。"""
        return self.message


class DownloadCancelled(Exception):
    """协作式取消下载的内部信号：由产出函数（如 yt-dlp 进度钩子）抛出以尽快中止。

    线程无法被强制终止，置位 ``threading.Event`` 后由产出函数在合适的时机（如下一次进度
    回调）抛出本异常，把“取消”收敛为一次普通异常，让线程尽早自然返回。
    """


def translate_network_error(exc: BaseException) -> MediaDownloadError:
    """把底层网络异常收敛为 ``MediaDownloadError``。

    仅依赖内置类型：``asyncio.TimeoutError`` 与 ``aiohttp.ServerTimeoutError``
    都派生自内置 ``TimeoutError``，其余归为网络失败。
    """
    if isinstance(exc, TimeoutError):
        return MediaDownloadError(FailureKind.TIMEOUT, "请求抖音服务超时，请稍后重试", detail=str(exc))
    return MediaDownloadError(FailureKind.NETWORK, "连接抖音服务失败，请稍后重试", detail=str(exc))


def part_path(target: Path) -> Path:
    """返回目标文件对应的临时文件路径（``<name>.part``）。"""
    return target.with_name(f"{target.name}{_PART_SUFFIX}")


def is_complete_mp4(path: Path) -> bool:
    """判断 ``path`` 是否为已落盘、非空且带 MP4 头的成品文件。

    ``.part`` 等临时文件、空文件、缺少 ``ftyp`` 头的损坏文件一律返回 ``False``。
    """
    if path.name.endswith(_PART_SUFFIX) or not path.is_file():
        return False
    try:
        if path.stat().st_size <= 0:
            return False
        with path.open("rb") as handle:
            head = handle.read(_HEAD_PROBE_BYTES)
    except OSError:
        return False
    return _FTYP in head


def find_completed_media(temp_dir: Path, stem: str, suffix: str = ".mp4") -> Path | None:
    """仅在存在精确命名的完成态成品文件时返回其路径。

    使用精确文件名（而非 glob）判定，因此 ``<stem>.mp4.part``、``<stem>.f137.mp4``
    等临时/分片文件永远不会被当作缓存命中。
    """
    candidate = temp_dir / f"{stem}{suffix}"
    return candidate if is_complete_mp4(candidate) else None


def ensure_complete_mp4(path: Path) -> None:
    """成品校验：不满足完成态时抛出 ``MediaDownloadError``。"""
    if not is_complete_mp4(path):
        raise MediaDownloadError(FailureKind.INVALID, "视频文件不完整或已损坏，请稍后重试", detail=str(path))


def probe_mp4(path: Path, *, timeout: float = 10.0) -> bool:
    """在具备 ``ffprobe`` 时做一次容器探测，否则退回纯头部校验。

    探测工具本身不可用（未安装、超时等）时回退为头部校验结果，避免因缺少工具而误判失败。
    """
    if not is_complete_mp4(path):
        return False
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return True
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode == 0 and b"video" in (result.stdout or b"")


def ensure_playable_mp4(path: Path) -> None:
    """成品可播放性校验：具备 ``ffprobe`` 时做容器探测，否则退回头部校验。

    不通过即抛 ``MediaDownloadError(INVALID)``，供暂存执行器在发布前拦截坏产物。
    """
    if not probe_mp4(path):
        raise MediaDownloadError(FailureKind.INVALID, "下载的视频校验未通过", detail=str(path))


def staging_dir(root: Path, key: str) -> Path:
    """为一次下载分配唯一暂存目录，隔离并发请求与迟到线程的写入。"""
    return root / f"{STAGING_PREFIX}{key}-{uuid4().hex}"


def locate_staged_media(work_dir: Path) -> Path | None:
    """在暂存目录中定位下载产物，忽略 ``.part``/``.ytdl`` 等中间文件。

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


def run_staged_download(
    temp_dir: Path,
    output_path: Path,
    key: str,
    produce: Callable[[Path, threading.Event], None],
    cancel_event: threading.Event,
    *,
    verify: Callable[[Path], None] = ensure_playable_mp4,
) -> bool:
    """在唯一暂存目录内完成一次「下载 → 校验 → 原子发布」，并清理暂存目录。

    顺序执行：建暂存目录 → ``produce`` 下载 → 取消检查 → 定位产物 → ``verify`` 校验 →
    取消检查 → 原子改名发布；``finally`` 中删除暂存目录。因为清理与下载/发布在**同一线程
    内顺序执行**，不存在「清理与 worker 并发」或「目录被删后 worker 重新落盘」；取消发生在
    发布前时丢弃暂存产物，保持「取消即无副作用」；成品路径只会经一次校验通过的原子改名写入。

    Args:
        temp_dir: 存放暂存目录的父目录。
        output_path: 最终成品路径。
        key: 命名暂存目录的稳定标识（如作品 id）。
        produce: 产出函数 ``(work_dir, cancel_event)``，在 ``work_dir`` 内落盘产物。
        cancel_event: 协作式取消信号。
        verify: 产物校验函数；不通过应抛 ``MediaDownloadError``。

    Returns:
        是否真正发布了成品；被取消或未产出有效文件时返回 ``False``。
    """
    work_dir = staging_dir(temp_dir, key)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        if cancel_event.is_set():
            return False
        produce(work_dir, cancel_event)
        if cancel_event.is_set():
            return False
        staged = locate_staged_media(work_dir)
        if staged is None:
            raise MediaDownloadError(FailureKind.INVALID, "下载的视频文件不完整", detail=str(work_dir))
        verify(staged)
        if cancel_event.is_set():
            return False
        staged.replace(output_path)
        return True
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def clean_stale_staging(temp_dir: Path, cutoff: float) -> None:
    """删除 ``temp_dir`` 下修改时间早于 ``cutoff`` 的下载暂存目录（兜底清理残留）。"""
    if not temp_dir.exists():
        return
    for path in temp_dir.iterdir():
        if path.is_dir() and path.name.startswith(STAGING_PREFIX) and path.stat().st_mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)


def _discard_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def atomic_output(target: Path) -> Iterator[Path]:
    """原子落盘上下文：先写临时文件，成功后再替换目标文件。

    用法::

        with atomic_output(target) as temp_path:
            temp_path.write_bytes(payload)  # 写入临时文件，校验也在此期间完成

    只要块内抛出任何异常（含 ``Cancel``/超时），临时文件都会被清理，目标文件保持不变。
    """
    temp = part_path(target)
    _discard_file(temp)
    try:
        yield temp
        temp.replace(target)
    except BaseException:
        _discard_file(temp)
        raise


async def run_with_retry(
    operation: Callable[[], Awaitable[Any]],
    *,
    max_retries: int = 2,
    backoff: tuple[float, ...] = (1.0, 3.0),
    on_retry: Callable[[MediaDownloadError, int, float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Any:
    """对可重试失败做固定上限的退避重试，非可重试原因立即抛出。

    Args:
        operation: 无参协程工厂，每次尝试都会重新调用。
        max_retries: 最大重试次数（不含首次尝试），必须为固定值，不做无限重试。
        backoff: 每次重试前的等待秒数序列；不足时复用最后一项，为空时退化为 1 秒。
        on_retry: 可选回调 ``(exc, attempt, delay)``，用于记录重试日志。
        should_stop: 可选短路判定；一旦为真立即停止重试并抛出当前错误（用于取消后
            不再发起新的尝试，避免取消信号下仍继续占用网络）。
    """
    attempt = 0
    while True:
        try:
            return await operation()
        except MediaDownloadError as exc:
            if (should_stop is not None and should_stop()) or not exc.retryable or attempt >= max_retries:
                raise
            delay = backoff[min(attempt, len(backoff) - 1)] if backoff else 1.0
            attempt += 1
            if on_retry is not None:
                on_retry(exc, attempt, delay)
            await asyncio.sleep(delay)


class InflightRegistry:
    """按 key 复用进行中的 asyncio 任务，任务结束后自动清理。

    用于“同一资源并发请求只下载一次”：进行中的任务被复用，任务结束（无论成功或失败）
    立刻释放条目，避免失败结果被永久复用、也避免后续请求复用已结束的任务。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def get(self, key: str) -> asyncio.Task[Any] | None:
        """返回进行中的任务；任务已结束则清理并返回 ``None``。"""
        task = self._tasks.get(key)
        if task is None:
            return None
        if task.done():
            self._tasks.pop(key, None)
            return None
        return task

    def register(self, key: str, task: asyncio.Task[Any]) -> None:
        """登记任务并在其结束时自动清理该 key。"""
        self._tasks[key] = task
        task.add_done_callback(lambda finished, _key=key: self._release(_key, finished))

    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(self._tasks.values())

    def clear(self) -> None:
        self._tasks.clear()

    def _release(self, key: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if not task.cancelled():
            task.exception()  # 消费异常，避免 “Task exception was never retrieved” 告警


class DownloadManager:
    """按 key 去重的后台下载任务登记表 + 协作式取消收尾。

    组合 :class:`InflightRegistry` 完成“同一资源并发请求只下载一次”；为每个进行中的任务
    维护一个 ``threading.Event`` 取消信号。``cancel_all()`` 先置位全部信号（协作式通知
    worker 尽快中止），再在**有界时间**内等待任务结束——因为 ``asyncio.to_thread`` 的等待只在
    底层线程退出时才 resolve，未被强制取消地等待任务即等价于等待 worker 真正退出；到达超时
    上限后才对剩余协程强制取消，随后才释放登记。

    如此可避免“取消后立即清空登记、放开同 id 重登”导致的“旧 worker 未退 + 同 id 新任务”
    并存：只要 worker 在有界时间内退出，登记就在其退出后才释放。
    """

    def __init__(self, name: str = "") -> None:
        self._name = name
        self._registry = InflightRegistry()
        self._events: dict[str, threading.Event] = {}

    def get(self, key: str) -> asyncio.Task[Any] | None:
        """返回进行中的任务；任务已结束则返回 ``None``。"""
        return self._registry.get(key)

    def event(self, key: str) -> threading.Event | None:
        """返回该 key 当前任务的协作式取消信号。"""
        return self._events.get(key)

    def schedule(self, key: str, factory: Callable[[threading.Event], Awaitable[Any]]) -> asyncio.Task[Any]:
        """登记并启动后台任务；同一 key 进行中时复用，否则新建并分配取消信号。

        Args:
            key: 去重键（如作品 id）。
            factory: 接收取消信号并返回协程的工厂；每次新建任务时调用一次。
        """
        existing = self._registry.get(key)
        if existing is not None:
            return existing
        event = threading.Event()
        self._events[key] = event
        task = asyncio.create_task(factory(event), name=f"{self._name}-{key}" if self._name else key)
        self._registry.register(key, task)
        task.add_done_callback(lambda _finished, _key=key: self._forget(_key))
        return task

    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return self._registry.tasks()

    async def cancel_all(self, *, grace: float = 5.0) -> None:
        """协作式取消全部进行中任务，在有界时间内等待 worker 退出，然后释放登记。

        ``grace`` 为等待 worker 退出的上限秒数；到点仍未结束的任务会被强制取消协程
        （线程本身无法强杀，但其已收到取消信号、会在下次机会自行收敛）。
        """
        tasks = self._registry.tasks()
        for event in tuple(self._events.values()):
            event.set()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=grace)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._events.clear()
        self._registry.clear()

    def _forget(self, key: str) -> None:
        # 完成回调与 InflightRegistry 的释放回调同批执行、互不穿插，故此处按 key 清除信号即可。
        self._events.pop(key, None)
