from __future__ import annotations

from pathlib import Path

from nekro_agent.api.plugin import CmdCtl, CommandExecutionContext, CommandPermission, CommandResponse

from . import MEDIA_CACHE, generated_temp_files, plugin
from .bilibili.downloader import cancel_download_tasks


@plugin.mount_command(
    name="clear_media",
    aliases=["清理媒体缓存", "清空视频缓存"],
    description="清理媒体与 Bilibili 临时文件",
    permission=CommandPermission.SUPER_USER,
    category="系统维护",
)
async def handle_clear_media_cache(_context: CommandExecutionContext) -> CommandResponse:
    await cancel_download_tasks()
    media_count = _clear_files(plugin.get_plugin_data_dir() / "media")
    bili_count = _clear_files(plugin.get_plugin_data_dir() / "bilibili")
    for path in tuple(generated_temp_files):
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    MEDIA_CACHE.clear()
    generated_temp_files.clear()
    return CmdCtl.success(f"媒体缓存清理完成。媒体文件：{media_count} 个，Bilibili 文件：{bili_count} 个。")


def _clear_files(directory: Path) -> int:
    if not directory.exists():
        return 0
    count = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            path.unlink()
        except OSError:
            continue
        count += 1
    return count
