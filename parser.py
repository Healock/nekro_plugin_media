from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from nonebot.exception import AdapterException
from nekro_agent.api import core

from .gemini import GeminiClient
from .media_io import MediaIO
from .prompts import build_media_prompt
from .types import MediaCacheEntry, MediaStatus, MediaType


class MediaParser:
    def __init__(
        self,
        api_key: str,
        model: str,
        temp_dir: Path,
        generated_files: set[Path],
    ) -> None:
        self.io = MediaIO(temp_dir, generated_files)
        self.gemini = GeminiClient(api_key, model)

    async def resolve_source(
        self,
        entry: MediaCacheEntry,
        bot: Any = None,
        *,
        allow_napcat_fallback: bool = False,
        napcat_temp_dir: Path | None = None,
    ) -> str:
        target = entry.url
        if not target and entry.local_file:
            target = entry.local_file
        if not target and entry.file_id and bot is not None:
            try:
                response = await bot.call_api("get_file", file_id=entry.file_id, timeout=120)
            except (AdapterException, RuntimeError, TypeError, ValueError) as exc:
                if not allow_napcat_fallback:
                    raise RuntimeError(f"【文件地址获取失败】{exc}") from exc
                target = await self._wait_napcat_file(entry.file_name, napcat_temp_dir)
            else:
                target = response.get("url") or response.get("file")
        if not target:
            raise RuntimeError("【媒体地址缺失】未能获取合法的媒体地址。")
        return str(target)

    async def process(
        self,
        entry: MediaCacheEntry,
        user_focus: str = "",
        bot: Any = None,
        *,
        allow_napcat_fallback: bool = False,
        napcat_temp_dir: Path | None = None,
    ) -> str:
        source = await self.resolve_source(
            entry, bot, allow_napcat_fallback=allow_napcat_fallback, napcat_temp_dir=napcat_temp_dir
        )
        suffix = entry.ext or (".mp4" if entry.type is MediaType.VIDEO else ".amr")
        local_path = await self.io.download(source, suffix)
        if entry.type is MediaType.AUDIO:
            local_path = await _to_thread(self.io.transcode_audio, local_path)
            mime_type = "audio/mp3"
        else:
            local_path = await _to_thread(self.io.transcode_video, local_path)
            mime_type = "video/mp4"
        prompt = build_media_prompt(entry.type, entry.sender_name, entry.context_text, user_focus)
        result = await self.gemini.generate(local_path, mime_type, prompt)
        return result

    async def process_video_background(
        self,
        media_id: str,
        entry: MediaCacheEntry,
        bot: Any,
        on_complete: Callable[[str, str], Awaitable[None]] | None = None,
        on_error: Callable[[str, Exception], Awaitable[None]] | None = None,
        napcat_temp_dir: Path | None = None,
    ) -> None:
        entry.status = MediaStatus.PROCESSING
        try:
            result = await self.process(
                entry,
                entry.agent_message,
                bot,
                allow_napcat_fallback=True,
                napcat_temp_dir=napcat_temp_dir,
            )
        except Exception as exc:
            entry.status = MediaStatus.ERROR
            core.logger.error(f"[Media] 后台解析失败（{media_id}）：{exc}")
            if on_error:
                await on_error(media_id, exc)
            return
        entry.status = MediaStatus.DONE
        entry.result = result
        if on_complete:
            await on_complete(media_id, result)

    async def _wait_napcat_file(self, file_name: str | None, temp_dir: Path | None = None) -> str | None:
        if not file_name:
            return None
        temp_dir = temp_dir or Path("/app/.config/QQ/NapCat/temp")
        for _ in range(30):
            if temp_dir.exists():
                for path in temp_dir.iterdir():
                    if path.is_file() and file_name in path.name:
                        return str(path)
            await asyncio.sleep(2)
        return None


async def _to_thread(function: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(function, *args)
