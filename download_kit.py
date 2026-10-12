"""通用媒体下载工具：完成态判定、原子落盘、并发去重、有限重试与统一异常。

本模块刻意只依赖标准库（`asyncio` / `pathlib` / `subprocess` 等），不引入
`aiohttp`、`nekro_agent` 或机器人框架，以便“失败处理”这类核心逻辑能被独立单元测试覆盖，
并可同时被 Bilibili、抖音等子模块复用（避免每个解析器各修一套、把同样的坑换个地方）。

设计要点：
- **完成态判定**：只有落盘的成品文件（文件名精确匹配且含 `ftyp` box）才算命中，
  `.part` 及任何临时/分片文件永不作为缓存结果返回。
- **原子落盘**：先写 `<name>.part`，校验通过后再 `rename` 成最终文件；任何异常、
  取消或超时都会清理临时文件，绝不残留半截文件。
- **统一异常**：`MediaDownloadError` + `FailureKind` 枚举，区分可重试、鉴权、永久失败、
  本地失败与产物无效，供上层生成面向用户的提示。
- **有限重试**：仅对可重试原因做固定次数、固定退避的重试，绝不无限重试。
"""

from __future__ import annotations

import asyncio
import enum
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = [
    "FailureKind",
    "InflightRegistry",
    "MediaDownloadError",
    "atomic_output",
    "ensure_complete_mp4",
    "find_completed_media",
    "is_complete_mp4",
    "part_path",
    "probe_mp4",
    "run_with_retry",
    "translate_network_error",
]

# MP4 文件头中的 'ftyp' box 类型标识；正常 mp4 的前若干字节内必然出现。
_FTYP = b"ftyp"
# 临时文件后缀：只有去掉该后缀的成品文件才对上层可见。
_PART_SUFFIX = ".part"
# 头部探测读取的字节数，足以覆盖 ftyp box（通常位于偏移 4）。
_HEAD_PROBE_BYTES = 64


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
) -> Any:
    """对可重试失败做固定上限的退避重试，非可重试原因立即抛出。

    Args:
        operation: 无参协程工厂，每次尝试都会重新调用。
        max_retries: 最大重试次数（不含首次尝试），必须为固定值，不做无限重试。
        backoff: 每次重试前的等待秒数序列；不足时复用最后一项，为空时退化为 1 秒。
        on_retry: 可选回调 ``(exc, attempt, delay)``，用于记录重试日志。
    """
    attempt = 0
    while True:
        try:
            return await operation()
        except MediaDownloadError as exc:
            if not exc.retryable or attempt >= max_retries:
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
