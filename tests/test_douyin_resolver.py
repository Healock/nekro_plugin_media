from __future__ import annotations

import asyncio

from nekro_plugin_media.download_kit import MediaDownloadError, FailureKind
from nekro_plugin_media.douyin import resolver


def test_dtk_failure_falls_back_to_native_once(monkeypatch) -> None:
    calls: list[str] = []

    async def immediate(operation):
        return await operation()

    async def fake_resolve(_url):
        calls.append("id")
        return "123456789012345"

    async def fake_dtk(_url, _config):
        calls.append("dtk")
        raise MediaDownloadError(FailureKind.AUTH, "dtk unavailable")

    async def fake_native(_aweme_id):
        calls.append("native")
        return object()

    monkeypatch.setattr(resolver, "_retry", immediate)
    monkeypatch.setattr(resolver, "resolve_aweme_id", fake_resolve)
    monkeypatch.setattr(resolver, "fetch_dtk_context", fake_dtk)
    monkeypatch.setattr(resolver, "fetch_douyin_context", fake_native)

    class Config:
        douyin_api_provider = "dtk"
        douyin_api_base_url = "https://demo.douyin.wtf"

    result = asyncio.run(resolver.resolve_context("https://www.douyin.com/video/123456789012345", Config()))
    assert result[0] == "123456789012345"
    assert calls == ["id", "dtk", "native"]


def test_dtk_success_does_not_call_native(monkeypatch) -> None:
    calls: list[str] = []

    async def immediate(operation):
        return await operation()

    async def fake_resolve(_url):
        calls.append("id")
        return "123456789012345"

    async def fake_dtk(_url, _config):
        calls.append("dtk")
        return object()

    async def fake_native(_aweme_id):
        calls.append("native")
        return object()

    monkeypatch.setattr(resolver, "_retry", immediate)
    monkeypatch.setattr(resolver, "resolve_aweme_id", fake_resolve)
    monkeypatch.setattr(resolver, "fetch_dtk_context", fake_dtk)
    monkeypatch.setattr(resolver, "fetch_douyin_context", fake_native)

    class Config:
        douyin_api_provider = "dtk"
        douyin_api_base_url = "https://demo.douyin.wtf"

    asyncio.run(resolver.resolve_context("https://www.douyin.com/video/123456789012345", Config()))
    assert calls == ["id", "dtk"]
