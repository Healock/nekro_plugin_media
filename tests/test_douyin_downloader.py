"""抖音下载器失败路径集成测试。

直接加载 ``douyin/downloader.py``（通过预注册包桩跳过会引入 nonebot 的
``douyin/__init__.py``），并用轻量 ``nekro_agent`` 桩替代运行时依赖，
从而在没有真实 NekroAgent / 网络的环境下覆盖：缓存命中判定、并发去重、
降级链与失败传播。真实网络与 aiohttp 传输层不在本测试范围内。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

PLUGIN_DIR = Path(__file__).resolve().parents[1]

_FTYP_HEADER = b"\x00\x00\x00\x18ftypisom"


def _install_nekro_stub() -> None:
    if importlib.util.find_spec("nekro_agent") is not None:
        return
    package = types.ModuleType("nekro_agent")
    api = types.ModuleType("nekro_agent.api")
    core = types.ModuleType("nekro_agent.api.core")
    core.logger = logging.getLogger("nekro_agent.stub")
    core.logger.success = core.logger.info  # Nekro 日志器提供 success，标准 logging 没有
    plugin = types.ModuleType("nekro_agent.api.plugin")

    def dynamic_import_pkg(spec: str, import_name: str | None = None, **_kwargs: object) -> object:
        return importlib.import_module(import_name or spec.split(">=")[0].strip())

    plugin.dynamic_import_pkg = dynamic_import_pkg
    api.core = core
    api.plugin = plugin
    package.api = api
    for name, module in {
        "nekro_agent": package,
        "nekro_agent.api": api,
        "nekro_agent.api.core": core,
        "nekro_agent.api.plugin": plugin,
    }.items():
        sys.modules.setdefault(name, module)


def _ensure_package_stub(name: str, path: Path) -> None:
    """注册一个仅有 __path__ 的包，避免执行真实的包 __init__（其中会导入 nonebot）。"""
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module


_install_nekro_stub()
_ensure_package_stub("nekro_plugin_media", PLUGIN_DIR)
_ensure_package_stub("nekro_plugin_media.douyin", PLUGIN_DIR / "douyin")

downloader = importlib.import_module("nekro_plugin_media.douyin.downloader")
kit = importlib.import_module("nekro_plugin_media.download_kit")


def test_find_cached_file_ignores_part(tmp_path: Path) -> None:
    (tmp_path / "123.mp4.part").write_bytes(_FTYP_HEADER)
    assert downloader.find_cached_file(tmp_path, "123") is None
    (tmp_path / "123.mp4").write_bytes(_FTYP_HEADER)
    found = downloader.find_cached_file(tmp_path, "123")
    assert found is not None
    assert found.name == "123.mp4"


def test_schedule_download_dedupes_inflight_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_download(*_args: object, **_kwargs: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(downloader, "_download", fake_download)
    out_dir = Path("/tmp/nekro-douyin-test")

    async def scenario() -> None:
        out = out_dir / "123.mp4"
        first = downloader.schedule_download("123", out, "https://v", "720p", out_dir)
        await started.wait()
        second = downloader.schedule_download("123", out, "https://v", "720p", out_dir)
        assert second is first  # 进行中复用同一任务
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0)
        # 任务结束后条目被清理，可再次启动新任务
        third = downloader.schedule_download("123", out, "https://v", "720p", out_dir)
        assert third is not first
        await asyncio.gather(third, return_exceptions=True)

    asyncio.run(scenario())


def test_schedule_download_releases_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(*_args: object, **_kwargs: object) -> None:
        raise kit.MediaDownloadError(kit.FailureKind.NETWORK, "boom")

    monkeypatch.setattr(downloader, "_download", failing)
    out_dir = Path("/tmp/nekro-douyin-test")

    async def scenario() -> None:
        out = out_dir / "9.mp4"
        first = downloader.schedule_download("9", out, "https://v", "720p", out_dir)
        with pytest.raises(kit.MediaDownloadError):
            await first
        await asyncio.sleep(0)
        second = downloader.schedule_download("9", out, "https://v", "720p", out_dir)
        assert second is not first  # 失败结果不被永久复用
        with pytest.raises(kit.MediaDownloadError):
            await second

    asyncio.run(scenario())


def test_download_propagates_permanent_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {"n": 0}

    async def permanent(*_args: object, **_kwargs: object) -> None:
        calls["n"] += 1
        raise kit.MediaDownloadError(kit.FailureKind.UNAVAILABLE, "gone")

    monkeypatch.setattr(downloader, "_download_direct", permanent)
    monkeypatch.setattr(downloader, "_RETRY_BACKOFF", (0.0, 0.0))

    async def scenario() -> None:
        with pytest.raises(kit.MediaDownloadError):
            await downloader._download(
                "42", tmp_path / "42.mp4", "https://v/nowm", "https://v/wm", "720p", tmp_path
            )
        assert calls["n"] == 1  # 永久失败既不重试也不降级

    asyncio.run(scenario())


def test_download_falls_back_to_watermark_tier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    async def fake_direct(url: str, output_path: Path, _referer: str) -> None:
        seen.append(url)
        if "playwm" not in url:
            raise kit.MediaDownloadError(kit.FailureKind.NETWORK, "transient")
        output_path.write_bytes(_FTYP_HEADER)  # 模拟带水印档位成功落盘

    def fake_ytdlp(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("yt-dlp 不可用")

    monkeypatch.setattr(downloader, "_download_direct", fake_direct)
    monkeypatch.setattr(downloader, "_download_with_ytdlp", fake_ytdlp)
    monkeypatch.setattr(downloader, "_RETRY_BACKOFF", (0.0, 0.0))

    async def scenario() -> None:
        out = tmp_path / "7.mp4"
        await downloader._download("7", out, "https://v/play", "https://v/playwm", "720p", tmp_path)
        assert out.exists()

    asyncio.run(scenario())
    assert "https://v/playwm" in seen  # 命中第三档位
