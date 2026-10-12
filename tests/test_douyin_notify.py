"""抖音失败通知与去重测试。

直接加载 ``douyin/notify.py``（通过预注册包桩跳过会引入 nonebot 的 ``douyin/__init__.py``），
用轻量 ``nekro_agent`` 桩替代运行时依赖，覆盖：会话标识提取、用户通知的容错，以及
“同一会话对同一失败任务只收到一条提示、不同会话各自收到、后续新任务仍会提示”的去重语义。
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

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _install_nekro_stub() -> None:
    if importlib.util.find_spec("nekro_agent") is not None:
        return
    package = types.ModuleType("nekro_agent")
    api = types.ModuleType("nekro_agent.api")
    core = types.ModuleType("nekro_agent.api.core")
    core.logger = logging.getLogger("nekro_agent.stub")
    core.logger.success = core.logger.info
    api.core = core
    package.api = api
    for name, module in {
        "nekro_agent": package,
        "nekro_agent.api": api,
        "nekro_agent.api.core": core,
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
_ensure_package_stub("nekro_plugin_media.douyin", PLUGIN_DIR / "douyin")

notify = importlib.import_module("nekro_plugin_media.douyin.notify")
kit = importlib.import_module("nekro_plugin_media.download_kit")


class _FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail = fail

    async def send(self, _event: object, text: str) -> None:
        if self._fail:
            raise RuntimeError("send boom")
        self.sent.append(text)


class _FakeEvent:
    def __init__(self, session: str) -> None:
        self._session = session

    def get_session_id(self) -> str:
        return self._session


def test_session_key_prefers_get_session_id() -> None:
    assert notify.session_key(_FakeEvent("group-42")) == "group-42"


def test_session_key_falls_back_to_user_and_group() -> None:
    class WithUser:
        user_id = "u1"

    class WithGroup:
        group_id = "g1"

    class Empty:
        pass

    assert notify.session_key(WithUser()) == "u1"
    assert notify.session_key(WithGroup()) == "g1"
    assert notify.session_key(Empty()) == ""


def test_notify_user_sends_and_swallows_errors() -> None:
    async def scenario() -> list[str]:
        bot = _FakeBot()
        await notify.notify_user(bot, object(), "hi")      # 正常发送
        await notify.notify_user(None, object(), "ignore")  # bot 为空直接跳过
        await notify.notify_user(_FakeBot(fail=True), object(), "oops")  # 发送失败只记日志
        return bot.sent

    assert asyncio.run(scenario()) == ["hi"]


def test_claim_dedupes_per_task_and_session() -> None:
    async def scenario() -> None:
        mapping = notify.FailureNotifier()
        first = asyncio.create_task(asyncio.sleep(0))
        second = asyncio.create_task(asyncio.sleep(0))
        assert mapping.claim(first, "s1") is True
        assert mapping.claim(first, "s1") is False      # 同任务同会话只声明一次
        assert mapping.claim(first, "s2") is True       # 同任务不同会话各自可声明
        assert mapping.claim(second, "s1") is True      # 新任务同会话可再次声明
        await asyncio.gather(first, second)

    asyncio.run(scenario())


def test_watch_download_notifies_once_per_session_but_not_across_sessions() -> None:
    async def scenario() -> list[str]:
        bot = _FakeBot()
        same_session_a = _FakeEvent("sess-1")
        same_session_b = _FakeEvent("sess-1")  # 同会话重复发送同一链接
        other_session = _FakeEvent("sess-2")

        async def failing() -> None:
            raise kit.MediaDownloadError(kit.FailureKind.NETWORK, "boom")

        task = asyncio.create_task(failing())
        # 同一任务注册三次回调（sess-1 两次、sess-2 一次）——模拟并发复用同一 inflight 任务。
        notify.watch_download(task, "123", bot, same_session_a)
        notify.watch_download(task, "123", bot, same_session_b)
        notify.watch_download(task, "123", bot, other_session)
        with pytest.raises(kit.MediaDownloadError):
            await task
        for _ in range(5):
            await asyncio.sleep(0)  # 冲刷 done 回调与其中 create_task 的通知协程

        # 后续用户主动重试：新任务、同会话，应再次收到提示（避免静默）。
        retry = asyncio.create_task(failing())
        notify.watch_download(retry, "123", bot, same_session_a)
        with pytest.raises(kit.MediaDownloadError):
            await retry
        for _ in range(5):
            await asyncio.sleep(0)
        return bot.sent

    sent = asyncio.run(scenario())
    # sess-1 一次 + sess-2 一次 + 重试一次 = 3 条（同会话首次的重复回调被去重）。
    assert len(sent) == 3
    assert all("【抖音下载失败】123" in text for text in sent)
