from __future__ import annotations

import asyncio
import os

from nonebot.matcher import matchers

from . import MEDIA_CACHE, background_tasks, generated_temp_files, plugin
from .bilibili.registration import cleanup as cleanup_bilibili
from .matcher import media_interceptor


@plugin.mount_cleanup_method()
async def cleanup() -> None:
    for task in tuple(background_tasks):
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    background_tasks.clear()
    await cleanup_bilibili()
    for path in list(generated_temp_files):
        try:
            if path.exists():
                os.remove(path)
        except OSError:
            continue
    generated_temp_files.clear()
    MEDIA_CACHE.clear()
    for priority, registered in list(matchers.items()):
        remaining = [matcher for matcher in registered if matcher is not media_interceptor]
        if remaining:
            matchers[priority] = remaining
        else:
            matchers.pop(priority, None)
