from __future__ import annotations

import asyncio
import shutil
import subprocess
import urllib.parse
import uuid
from pathlib import Path

import aiohttp
from nekro_agent.api import core


class MediaIO:
    def __init__(self, temp_dir: Path, generated_files: set[Path]) -> None:
        self.temp_dir = temp_dir
        self.generated_files = generated_files
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def download(self, source: str, suffix: str) -> Path:
        target = self.temp_dir / f"{uuid.uuid4().hex}{suffix}"
        decoded = urllib.parse.unquote(source)
        if _is_local(decoded):
            local = Path(decoded)
            for _ in range(150):
                if local.exists() and local.stat().st_size > 0:
                    await asyncio.to_thread(shutil.copyfile, local, target)
                    self.generated_files.add(target)
                    return target
                await asyncio.sleep(2)
            raise FileNotFoundError(f"媒体文件未就绪：{decoded}")

        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(source) as response:
                response.raise_for_status()
                with target.open("wb") as output:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        if chunk:
                            output.write(chunk)
        self.generated_files.add(target)
        return target

    def transcode_audio(self, source: Path) -> Path:
        if source.suffix.lower() == ".mp3":
            return source
        target = source.with_suffix(".mp3")
        self._run_ffmpeg(["-acodec", "libmp3lame", "-ar", "44100", "-b:a", "128k"], source, target)
        return target

    def transcode_video(self, source: Path) -> Path:
        target = source.with_name(f"standardized_{source.name}.mp4")
        try:
            self._run_ffmpeg(
                ["-c:v", "libx264", "-preset", "fast", "-crf", "28", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"],
                source,
                target,
            )
        except FileNotFoundError:
            core.logger.warning("[Media] ffmpeg 不可用，回退原始视频文件")
            return source
        return target

    def _run_ffmpeg(self, options: list[str], source: Path, target: Path) -> None:
        command = ["ffmpeg", "-y", "-i", str(source), *options, "-loglevel", "error", str(target)]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise FileNotFoundError("ffmpeg_error") from exc
        self.generated_files.add(target)


def _is_local(value: str) -> bool:
    return value.startswith("/") or (len(value) > 2 and value[1] == ":")
