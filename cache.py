from __future__ import annotations

import time
from collections.abc import Iterator, MutableMapping
from typing import Any

from .types import MediaCacheEntry, MediaStatus


class MediaCache(MutableMapping[str, MediaCacheEntry]):
    def __init__(self) -> None:
        self._entries: dict[str, MediaCacheEntry] = {}

    def __getitem__(self, key: str) -> MediaCacheEntry:
        return self._entries[key]

    def __setitem__(self, key: str, value: MediaCacheEntry | dict[str, Any]) -> None:
        self._entries[key] = value if isinstance(value, MediaCacheEntry) else MediaCacheEntry.from_legacy(value)
        self.prune()

    def prune(self, *, max_age_seconds: int = 86400, max_entries: int = 256) -> None:
        cutoff = time.time() - max_age_seconds
        for key, entry in list(self._entries.items()):
            if entry.timestamp < cutoff:
                self._entries.pop(key, None)
        if len(self._entries) > max_entries:
            ordered = sorted(self._entries.items(), key=lambda item: item[1].timestamp)
            for key, _entry in ordered[: len(self._entries) - max_entries]:
                self._entries.pop(key, None)

    def __delitem__(self, key: str) -> None:
        del self._entries[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def get_entry(self, media_id: str) -> MediaCacheEntry | None:
        self.prune()
        return self._entries.get(media_id)

    def set_status(
        self,
        media_id: str,
        status: MediaStatus,
        *,
        result: str | None = None,
    ) -> MediaCacheEntry:
        entry = self._entries[media_id]
        entry.status = status
        if result is not None:
            entry.result = result
        return entry


MEDIA_CACHE = MediaCache()
