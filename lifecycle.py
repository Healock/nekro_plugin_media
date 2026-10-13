from __future__ import annotations

import asyncio
import os

from . import MEDIA_CACHE, background_tasks, generated_temp_files, plugin
from . import matcher as media_matcher
from .bilibili import matcher as bilibili_matcher
from .douyin import matcher as douyin_matcher
from .bilibili.registration import cleanup as cleanup_bilibili
from .douyin.registration import cleanup as cleanup_douyin
from .matcher_cleanup import destroy_matcher


@plugin.mount_init_method()
async def initialize_matchers() -> None:
    """Recreate matchers after a plugin hot reload destroyed old instances."""

    bilibili_matcher.register_matcher()
    douyin_matcher.register_matcher()
    media_matcher.register_matcher()


@plugin.mount_cleanup_method()
async def cleanup() -> None:
    for task in tuple(background_tasks):
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    background_tasks.clear()
    await cleanup_bilibili()
    await cleanup_douyin()
    for path in list(generated_temp_files):
        try:
            if path.exists():
                os.remove(path)
        except OSError:
            continue
    generated_temp_files.clear()
    MEDIA_CACHE.clear()
    destroy_matcher(media_matcher.media_interceptor)
    media_matcher.media_interceptor = None
