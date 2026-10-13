"""抖音下载失败的用户通知与去重。

下载失败必须向来源聊天推送一条可读提示（不静默），但同一 ``aweme_id`` 的并发请求会复用
同一后台任务（见 ``downloader.schedule_download``），一次失败会被多个等待方各注册一次回调；
若同一会话重复发送同一链接，就会收到多条相同的告警。

去重语义：
- **同一会话**对**同一次下载尝试**只收到一条失败提示（避免刷屏）；
- **不同会话**各自仍能收到（每个等待方都应知情，属期望行为）；
- 用户稍后主动重试会发起**新的**下载任务，因此仍会再次收到提示（避免静默）。

实现按“下载任务 + 会话”去重，用 ``WeakKeyDictionary`` 以任务为键：任务结束并被回收后
条目自动消失，天然有界，也无需手工清理。
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from typing import Any

from nekro_agent.api import core

from ..download_kit import MediaDownloadError

__all__ = ["FailureNotifier", "notifier", "notify_user", "session_key", "watch_download"]


def session_key(event: object) -> str:
    """尽力提取来源会话标识，优先使用 OneBot 的 ``get_session_id()``。"""
    getter = getattr(event, "get_session_id", None)
    if callable(getter):
        with contextlib.suppress(Exception):
            return str(getter())
    return str(getattr(event, "user_id", "") or getattr(event, "group_id", "") or "")


async def notify_user(bot: Any, event: Any, text: str) -> None:
    """向来源聊天发送一条用户可见的提示；发送本身失败只记日志，不再抛出。"""
    if bot is None:
        return
    try:
        await bot.send(event, text)
    except Exception as exc:  # noqa: BLE001 - 通知失败不应影响主流程
        core.logger.warning(f"[Douyin] 失败提示发送失败：{exc}")


class FailureNotifier:
    """按“下载任务 + 会话”对失败通知去重。

    同一任务内，每个会话只允许声明一次通知；``claim`` 首次返回 ``True``，重复返回 ``False``。
    """

    def __init__(self) -> None:
        self._notified: weakref.WeakKeyDictionary[asyncio.Task[Any], set[str]] = (
            weakref.WeakKeyDictionary()
        )

    def claim(self, task: asyncio.Task[Any], session: str) -> bool:
        """首次为 ``(task, session)`` 声明通知时返回 ``True``，重复声明返回 ``False``。"""
        sessions = self._notified.get(task)
        if sessions is None:
            sessions = set()
            self._notified[task] = sessions
        if session in sessions:
            return False
        sessions.add(session)
        return True


# 进程内单例：下载任务的失败通知统一经此去重。
notifier = FailureNotifier()


def watch_download(task: asyncio.Task[Any], aweme_id: str, bot: Any, event: Any) -> None:
    """监听后台下载任务，失败时向发送者推送一条可读原因（同会话去重）。

    同一 ``aweme_id`` 的并发请求会复用同一后台任务，因此可能对同一任务重复注册回调；
    这里按 ``(任务, 会话)`` 去重，保证同一会话在一次下载失败中只收到一条提示，而不同
    会话各自仍会收到；用户稍后主动重试是新任务，仍会再次收到提示（不静默）。
    """
    if bot is None:
        return
    session = session_key(event)

    def _done(finished: asyncio.Task[Any]) -> None:
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is None:
            return
        if not notifier.claim(finished, session):
            return  # 同一会话已就本任务告警过，避免重复提示
        reason = exc.user_message() if isinstance(exc, MediaDownloadError) else "下载失败，请稍后重试"
        asyncio.create_task(
            notify_user(bot, event, f"【抖音下载失败】{aweme_id}\n{reason}\n可稍后重新发送该链接重试。")
        )

    task.add_done_callback(_done)
