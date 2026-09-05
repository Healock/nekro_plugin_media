from __future__ import annotations

import os
from typing import Any

from nonebot.exception import AdapterException
from nekro_agent.api import core

from .types import MediaType


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".amr", ".flac", ".m4a", ".ogg", ".silk"}


async def extract_media_info(bot: Any, raw_message: list[dict[str, Any]]) -> dict[str, Any] | None:
    for segment in raw_message:
        segment_type = segment.get("type")
        data = segment.get("data") or {}
        if segment_type in {"record", "video"}:
            raw_url = data.get("url") or data.get("file") or data.get("file_url")
            url = _local_path(raw_url) or raw_url
            file_id = data.get("file_id") or data.get("id")
            if url or file_id:
                return {
                    "url": url,
                    "file_id": file_id,
                    "local_file": data.get("file"),
                    "file_name": data.get("file_name") or data.get("name"),
                    "type": MediaType.VIDEO if segment_type == "video" else MediaType.AUDIO,
                    "ext": "",
                }

        if segment_type != "file":
            continue
        file_name = str(data.get("name") or data.get("file_name") or data.get("file") or "")
        url = data.get("url") or data.get("file_url")
        local_file = data.get("file")
        file_id = data.get("file_id") or data.get("id")
        valid_path = _local_path(url) or url or _local_path(local_file)
        if not valid_path and file_id and bot:
            try:
                response = await bot.call_api("get_file", file_id=file_id)
            except (AdapterException, RuntimeError, TypeError, ValueError) as exc:
                core.logger.warning(f"[Media] 获取文件地址失败：{exc}")
            else:
                valid_path = response.get("url") or _local_path(response.get("file"))
        if not valid_path:
            continue

        ext = os.path.splitext(file_name.lower())[1]
        if ext in VIDEO_EXTENSIONS or ext in AUDIO_EXTENSIONS:
            return {
                "url": valid_path,
                "file_id": file_id,
                "local_file": local_file,
                "file_name": file_name,
                "type": MediaType.VIDEO if ext in VIDEO_EXTENSIONS else MediaType.AUDIO,
                "ext": ext,
            }
    return None


def _local_path(value: Any) -> str | None:
    if not value:
        return None
    path = str(value)
    if path.startswith("/") or path.startswith("file://") or (len(path) > 2 and path[1] == ":"):
        return path.removeprefix("file://")
    return None
