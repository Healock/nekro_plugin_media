from __future__ import annotations

import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MediaType(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"


class MediaStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"


class MediaCacheEntry(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    url: str | None = None
    file_id: str | None = None
    local_file: str | None = None
    file_name: str | None = None
    type: MediaType
    ext: str = ""
    sender_name: str = "群友"
    context_text: str = ""
    message_id: int | str | None = None
    bot_id: str | None = None
    status: MediaStatus = MediaStatus.PENDING
    timestamp: float = Field(default_factory=time.time)
    chat_key: str | None = None
    agent_message: str = ""
    result: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    @classmethod
    def from_legacy(cls, value: dict[str, Any]) -> "MediaCacheEntry":
        """兼容旧版字典缓存。"""
        return cls.model_validate(value)

    def to_legacy(self) -> dict[str, Any]:
        """保留旧缓存字段名，供尚未迁移的调用方使用。"""
        return self.model_dump(mode="json", exclude_none=True)


MediaCacheData = dict[str, MediaCacheEntry]
MediaKind = Literal["audio", "video"]
