from __future__ import annotations

import asyncio
import os

from . import MEDIA_CACHE, background_tasks, generated_temp_files, plugin
from .bilibili.registration import cleanup as cleanup_bilibili
from .matcher import media_interceptor
from .matcher_cleanup import destroy_matcher


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
    destroy_matcher(media_interceptor)
