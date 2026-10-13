"""``download_kit`` 失败处理纯逻辑单元测试。

``download_kit.py`` 只依赖标准库，这里直接从文件加载，覆盖完成态判定、
原子落盘清理、有限重试与并发去重等易错逻辑，无需真实网络或机器人环境。
"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from pathlib import Path

import pytest

KIT_PATH = Path(__file__).resolve().parents[1] / "download_kit.py"

_FTYP_HEADER = b"\x00\x00\x00\x18ftypisom"


def _load_kit():
    spec = importlib.util.spec_from_file_location("_download_kit_under_test", KIT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kit = _load_kit()


# --- 完成态判定：.part / 空文件 / 损坏文件永不命中 -----------------------------------


def test_is_complete_mp4_accepts_valid_header(tmp_path: Path) -> None:
    target = tmp_path / "a.mp4"
    target.write_bytes(_FTYP_HEADER)
    assert kit.is_complete_mp4(target)


def test_is_complete_mp4_rejects_part_empty_and_garbage(tmp_path: Path) -> None:
    part = tmp_path / "a.mp4.part"
    part.write_bytes(_FTYP_HEADER)
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"this is definitely not a valid mp4 container")
    assert not kit.is_complete_mp4(part)
    assert not kit.is_complete_mp4(empty)
    assert not kit.is_complete_mp4(garbage)
    assert not kit.is_complete_mp4(tmp_path / "missing.mp4")


def test_find_completed_media_ignores_part_and_fragments(tmp_path: Path) -> None:
    (tmp_path / "id.mp4.part").write_bytes(_FTYP_HEADER)
    (tmp_path / "id.f137.mp4").write_bytes(_FTYP_HEADER)
    assert kit.find_completed_media(tmp_path, "id", ".mp4") is None


def test_find_completed_media_returns_complete_file(tmp_path: Path) -> None:
    (tmp_path / "id.mp4").write_bytes(_FTYP_HEADER)
    (tmp_path / "id.mp4.part").write_bytes(b"half written")
    found = kit.find_completed_media(tmp_path, "id", ".mp4")
    assert found is not None
    assert found.name == "id.mp4"


def test_ensure_complete_mp4_raises_on_invalid(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"nope")
    with pytest.raises(kit.MediaDownloadError) as excinfo:
        kit.ensure_complete_mp4(bad)
    assert excinfo.value.kind is kit.FailureKind.INVALID


# --- 原子落盘：失败/取消清理临时文件；成功才替换成品 ---------------------------------


def test_atomic_output_cleans_temp_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "a.mp4"
    with pytest.raises(RuntimeError), kit.atomic_output(target) as temp:
        assert temp.name == "a.mp4.part"
        temp.write_bytes(b"partial")
        raise RuntimeError("boom")
    assert not target.exists()
    assert not (tmp_path / "a.mp4.part").exists()


def test_atomic_output_cleans_temp_on_cancellation(tmp_path: Path) -> None:
    target = tmp_path / "a.mp4"

    async def scenario() -> None:
        def work() -> None:
            with kit.atomic_output(target) as temp:
                temp.write_bytes(b"partial")
                raise asyncio.CancelledError

        task = asyncio.create_task(asyncio.to_thread(work))
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not target.exists()
    assert not (tmp_path / "a.mp4.part").exists()


def test_atomic_output_renames_on_success(tmp_path: Path) -> None:
    target = tmp_path / "a.mp4"
    with kit.atomic_output(target) as temp:
        temp.write_bytes(_FTYP_HEADER)
    assert target.exists()
    assert not (tmp_path / "a.mp4.part").exists()
    assert kit.is_complete_mp4(target)


# --- 有限重试：仅可重试原因重试，固定上限 -------------------------------------------


def test_run_with_retry_succeeds_after_transient_failures() -> None:
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise kit.MediaDownloadError(kit.FailureKind.NETWORK, "boom")
        return "ok"

    result = asyncio.run(kit.run_with_retry(operation, max_retries=2, backoff=(0.0, 0.0)))
    assert result == "ok"
    assert calls["n"] == 3


def test_run_with_retry_stops_at_fixed_max() -> None:
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        raise kit.MediaDownloadError(kit.FailureKind.TIMEOUT, "timeout")

    with pytest.raises(kit.MediaDownloadError):
        asyncio.run(kit.run_with_retry(operation, max_retries=2, backoff=(0.0, 0.0)))
    assert calls["n"] == 3  # 首次 + 2 次重试，绝不无限重试


def test_run_with_retry_skips_permanent_failures() -> None:
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        raise kit.MediaDownloadError(kit.FailureKind.UNAVAILABLE, "gone")

    with pytest.raises(kit.MediaDownloadError):
        asyncio.run(kit.run_with_retry(operation, max_retries=2, backoff=(0.0, 0.0)))
    assert calls["n"] == 1


def test_failure_kind_retryable_flags() -> None:
    assert kit.FailureKind.NETWORK.retryable
    assert kit.FailureKind.SERVER.retryable
    assert kit.FailureKind.TIMEOUT.retryable
    assert not kit.FailureKind.AUTH.retryable
    assert not kit.FailureKind.UNAVAILABLE.retryable
    assert not kit.FailureKind.PARSE.retryable
    assert not kit.FailureKind.LOCAL.retryable
    assert not kit.FailureKind.INVALID.retryable


# --- 并发去重：进行中复用，结束后（含失败）释放 -------------------------------------


def test_inflight_registry_reuses_inflight_task() -> None:
    async def scenario() -> None:
        registry = kit.InflightRegistry()
        started = asyncio.Event()
        release = asyncio.Event()

        async def job() -> None:
            started.set()
            await release.wait()

        task = asyncio.create_task(job())
        registry.register("k", task)
        await started.wait()
        assert registry.get("k") is task
        release.set()
        await task
        await asyncio.sleep(0)
        assert registry.get("k") is None

    asyncio.run(scenario())


def test_inflight_registry_releases_after_failure() -> None:
    async def scenario() -> None:
        registry = kit.InflightRegistry()

        async def job() -> None:
            raise kit.MediaDownloadError(kit.FailureKind.NETWORK, "boom")

        task = asyncio.create_task(job())
        registry.register("k", task)
        with pytest.raises(kit.MediaDownloadError):
            await task
        await asyncio.sleep(0)
        assert registry.get("k") is None

    asyncio.run(scenario())


def test_inflight_registry_drops_finished_task_on_get() -> None:
    async def scenario() -> None:
        registry = kit.InflightRegistry()

        async def job() -> None:
            return None

        task = asyncio.create_task(job())
        await task
        registry.register("k", task)  # 登记一个已完成任务
        assert registry.get("k") is None
        assert registry.tasks() == ()

    asyncio.run(scenario())


# --- 暂存执行器：唯一目录 + 校验后原子发布 + 失败/取消清理 --------------------------


def test_staging_dir_is_unique_and_prefixed(tmp_path: Path) -> None:
    first = kit.staging_dir(tmp_path, "id")
    second = kit.staging_dir(tmp_path, "id")
    assert first != second
    assert first.parent == tmp_path
    assert first.name.startswith(".ytdlp-id-")


def test_locate_staged_media_ignores_partials(tmp_path: Path) -> None:
    (tmp_path / "x.mp4.part").write_bytes(b"half")
    (tmp_path / "x.ytdl").write_bytes(b"frag")
    assert kit.locate_staged_media(tmp_path) is None
    (tmp_path / "x.mp4").write_bytes(_FTYP_HEADER)
    found = kit.locate_staged_media(tmp_path)
    assert found is not None
    assert found.name == "x.mp4"


def test_run_staged_download_publishes_and_cleans(tmp_path: Path) -> None:
    out = tmp_path / "out.mp4"

    def produce(work_dir: Path, _event: threading.Event) -> None:
        work_dir.joinpath("out.mp4").write_bytes(_FTYP_HEADER)

    published = kit.run_staged_download(
        tmp_path, out, "k", produce, threading.Event(), verify=kit.ensure_complete_mp4
    )
    assert published is True
    assert out.read_bytes() == _FTYP_HEADER
    assert list(tmp_path.glob(".ytdlp-*")) == []


def test_run_staged_download_cleans_on_failure(tmp_path: Path) -> None:
    out = tmp_path / "out.mp4"

    def produce(work_dir: Path, _event: threading.Event) -> None:
        work_dir.joinpath("out.mp4").write_bytes(_FTYP_HEADER)  # 半成品
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        kit.run_staged_download(tmp_path, out, "k", produce, threading.Event(), verify=kit.ensure_complete_mp4)
    assert not out.exists()
    assert list(tmp_path.glob(".ytdlp-*")) == []


def test_run_staged_download_skips_publish_when_cancelled(tmp_path: Path) -> None:
    out = tmp_path / "out.mp4"
    event = threading.Event()

    def produce(work_dir: Path, ev: threading.Event) -> None:
        work_dir.joinpath("out.mp4").write_bytes(_FTYP_HEADER)
        ev.set()  # 下载完成后、发布前发生取消

    published = kit.run_staged_download(tmp_path, out, "k", produce, event, verify=kit.ensure_complete_mp4)
    assert published is False
    assert not out.exists()
    assert list(tmp_path.glob(".ytdlp-*")) == []


def test_run_staged_download_discards_when_verify_fails(tmp_path: Path) -> None:
    out = tmp_path / "out.mp4"

    def produce(work_dir: Path, _event: threading.Event) -> None:
        work_dir.joinpath("out.mp4").write_bytes(_FTYP_HEADER)

    def reject(_path: Path) -> None:
        raise kit.MediaDownloadError(kit.FailureKind.INVALID, "bad")

    with pytest.raises(kit.MediaDownloadError) as excinfo:
        kit.run_staged_download(tmp_path, out, "k", produce, threading.Event(), verify=reject)
    assert excinfo.value.kind is kit.FailureKind.INVALID
    assert not out.exists()
    assert list(tmp_path.glob(".ytdlp-*")) == []


def test_run_staged_download_raises_invalid_when_no_media(tmp_path: Path) -> None:
    out = tmp_path / "out.mp4"

    def produce(_work_dir: Path, _event: threading.Event) -> None:
        return None  # 未产出任何文件

    with pytest.raises(kit.MediaDownloadError) as excinfo:
        kit.run_staged_download(tmp_path, out, "k", produce, threading.Event(), verify=kit.ensure_complete_mp4)
    assert excinfo.value.kind is kit.FailureKind.INVALID
    assert list(tmp_path.glob(".ytdlp-*")) == []


def test_clean_stale_staging_removes_only_old_staging_dirs(tmp_path: Path) -> None:
    stale = tmp_path / ".ytdlp-old-abcd"
    stale.mkdir()
    fresh = tmp_path / ".ytdlp-new-efgh"
    fresh.mkdir()
    keep = tmp_path / "important.mp4"
    keep.write_bytes(_FTYP_HEADER)
    stale.touch()
    import os

    old = time.time() - 3600
    os.utime(stale, (old, old))
    kit.clean_stale_staging(tmp_path, time.time() - 1800)  # 截止 30 分钟前
    assert not stale.exists()  # 1 小时前的暂存目录被清理
    assert fresh.exists()      # 新建的暂存目录保留
    assert keep.exists()       # 只清理暂存目录，不动成品文件


# --- 有限重试：取消短路 -------------------------------------------------------------


def test_run_with_retry_short_circuits_when_should_stop() -> None:
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        raise kit.MediaDownloadError(kit.FailureKind.NETWORK, "boom")

    with pytest.raises(kit.MediaDownloadError):
        asyncio.run(
            kit.run_with_retry(operation, max_retries=5, backoff=(0.0,), should_stop=lambda: True)
        )
    assert calls["n"] == 1  # should_stop 为真时不再重试


# --- 下载管理器：并发去重 + 取消有界等待 --------------------------------------------


def test_download_manager_reuses_inflight_and_clears_event_on_done() -> None:
    async def scenario() -> None:
        manager = kit.DownloadManager("test")
        started = asyncio.Event()
        release = asyncio.Event()

        async def job(_event: threading.Event) -> None:
            started.set()
            await release.wait()

        first = manager.schedule("k", job)
        await started.wait()
        assert manager.get("k") is first
        assert manager.event("k") is not None
        assert manager.schedule("k", job) is first  # 进行中复用
        release.set()
        await first
        await asyncio.sleep(0)
        assert manager.get("k") is None
        assert manager.event("k") is None  # 结束后信号被清理

    asyncio.run(scenario())


def test_download_manager_cancel_all_waits_for_cooperative_exit() -> None:
    async def scenario() -> None:
        manager = kit.DownloadManager("test")
        started = asyncio.Event()
        seen: dict[str, bool] = {}

        async def job(event: threading.Event) -> None:
            started.set()
            while not event.is_set():
                await asyncio.sleep(0.005)
            seen["event"] = True  # 观察到取消信号后自行退出

        task = manager.schedule("k", job)
        await started.wait()
        await manager.cancel_all(grace=2.0)
        assert seen.get("event") is True
        assert task.done()
        assert manager.get("k") is None

    asyncio.run(scenario())


def test_download_manager_cancel_all_force_cancels_after_grace() -> None:
    async def scenario() -> None:
        manager = kit.DownloadManager("test")
        started = asyncio.Event()

        async def stubborn(_event: threading.Event) -> None:
            started.set()
            await asyncio.sleep(30)  # 忽略取消信号，模拟无进度回调的长阻塞

        task = manager.schedule("k", stubborn)
        await started.wait()
        await asyncio.wait_for(manager.cancel_all(grace=0.05), timeout=2.0)
        assert task.cancelled()  # 到达有界上限后强制取消
        assert manager.get("k") is None

    asyncio.run(scenario())
