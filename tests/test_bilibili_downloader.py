"""Bilibili 下载器失败/原子发布回归测试。

直接加载 ``bilibili/downloader.py``（通过预注册包桩跳过 ``__init__``），用轻量
``nekro_agent`` 桩替代运行时依赖，覆盖本次收敛的重点：**失败残留绝不落成品路径、
因而不会被误判为缓存命中**，以及成功时经暂存目录原子发布并清理。

真实网络与 yt-dlp 传输层不在本测试范围内。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
import threading
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
    core.logger.success = core.logger.info
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
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module


_install_nekro_stub()
_ensure_package_stub("nekro_plugin_media", PLUGIN_DIR)
_ensure_package_stub("nekro_plugin_media.bilibili", PLUGIN_DIR / "bilibili")

downloader = importlib.import_module("nekro_plugin_media.bilibili.downloader")


def test_find_cached_file_ignores_part(tmp_path: Path) -> None:
    (tmp_path / "BV1.mp4.part").write_bytes(_FTYP_HEADER)
    assert downloader.find_cached_file(tmp_path, "BV1") is None
    (tmp_path / "BV1.mp4").write_bytes(_FTYP_HEADER)
    found = downloader.find_cached_file(tmp_path, "BV1")
    assert found is not None
    assert found.name == "BV1.mp4"


def test_failed_download_leaves_no_residue_and_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """失败（yt-dlp 写下半成品后抛错）绝不在成品路径留残留，因而不会被当缓存命中。

    这是本次收敛的核心回归：改动前 ``ignoreerrors=True`` + 直写 ``output_path``，失败残留
    非空且含 ``ftyp`` 头即会被 ``find_cached_file`` 命中，进而投递坏文件且不自愈。
    """

    def failing_ytdlp(
        bvid: str, out_tmpl: str, _format_str: str, _data_dir: Path, _cancel_event: threading.Event
    ) -> None:
        staged = Path(out_tmpl).parent
        staged.mkdir(parents=True, exist_ok=True)
        (staged / f"{bvid}.mp4").write_bytes(_FTYP_HEADER)  # 半成品（含 ftyp 头）
        raise RuntimeError("yt-dlp boom")

    monkeypatch.setattr(downloader, "_download_with_ytdlp", failing_ytdlp)

    media = tmp_path / "BV1.mp4"
    with pytest.raises(RuntimeError):
        downloader.background_download_task("BV1", media, "best", tmp_path, threading.Event())
    assert not media.exists()
    assert downloader.find_cached_file(tmp_path, "BV1") is None
    assert list(tmp_path.glob(".ytdlp-*")) == []


def test_successful_download_publishes_and_cleans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_ytdlp(
        bvid: str, out_tmpl: str, _format_str: str, _data_dir: Path, _cancel_event: threading.Event
    ) -> None:
        staged = Path(out_tmpl).parent
        staged.mkdir(parents=True, exist_ok=True)
        (staged / f"{bvid}.mp4").write_bytes(_FTYP_HEADER)

    monkeypatch.setattr(downloader, "_download_with_ytdlp", fake_ytdlp)
    # 用纯头部校验替代 ffprobe，避免依赖真实容器与外部工具。
    monkeypatch.setattr(downloader, "ensure_playable_mp4", lambda _path: None)

    media = tmp_path / "BV2.mp4"
    published = downloader.background_download_task("BV2", media, "best", tmp_path, threading.Event())
    assert published is True
    assert media.read_bytes() == _FTYP_HEADER
    assert downloader.find_cached_file(tmp_path, "BV2") is not None
    assert list(tmp_path.glob(".ytdlp-*")) == []


def test_download_skips_work_when_already_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = {"n": 0}

    def fake_ytdlp(*_args: object, **_kwargs: object) -> None:
        called["n"] += 1

    monkeypatch.setattr(downloader, "_download_with_ytdlp", fake_ytdlp)
    event = threading.Event()
    event.set()  # 下载前即已取消

    media = tmp_path / "BV5.mp4"
    published = downloader.background_download_task("BV5", media, "best", tmp_path, event)
    assert published is False
    assert called["n"] == 0  # 已取消则不再发起下载
    assert not media.exists()
    assert list(tmp_path.glob(".ytdlp-*")) == []


def test_schedule_download_dedupes_inflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking(
        bvid: str, output_path: Path, _format_str: str, _data_dir: Path, _cancel_event: threading.Event
    ) -> None:
        started.set()
        release.wait(timeout=5.0)

    monkeypatch.setattr(downloader, "background_download_task", blocking)

    async def scenario() -> None:
        media = tmp_path / "BV3.mp4"
        first = downloader.schedule_download("BV3", media, "best", tmp_path)
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        assert downloader.schedule_download("BV3", media, "best", tmp_path) is first  # 进行中复用
        release.set()
        await asyncio.gather(first, return_exceptions=True)

    asyncio.run(scenario())


def test_cancel_all_waits_for_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking(
        bvid: str, output_path: Path, _format_str: str, _data_dir: Path, _cancel_event: threading.Event
    ) -> None:
        started.set()
        release.wait(timeout=5.0)  # 不响应取消信号：模拟无进度回调的长阻塞

    monkeypatch.setattr(downloader, "background_download_task", blocking)

    async def scenario() -> None:
        media = tmp_path / "BV4.mp4"
        task = downloader.schedule_download("BV4", media, "best", tmp_path)
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        cancelling = asyncio.create_task(downloader.cancel_download_tasks())
        await asyncio.sleep(0.05)
        assert not cancelling.done(), "cancel_all 应等待 worker 退出"
        assert downloader._DOWNLOADS.get("BV4") is task

        release.set()
        await asyncio.wait_for(cancelling, timeout=5.0)
        assert downloader._DOWNLOADS.get("BV4") is None

    asyncio.run(scenario())
