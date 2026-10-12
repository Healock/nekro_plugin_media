from .downloader import cancel_download_tasks
from .lifecycle import cleanup_matcher


async def cleanup() -> None:
    await cancel_download_tasks()
    await cleanup_matcher()
