from __future__ import annotations

import asyncio

from nekro_plugin_media.download_kit import MediaDownloadError, FailureKind
from nekro_plugin_media.douyin import resolver


def test_dtk_failure_falls_back_through_signed_guest_to_share_page(monkeypatch) -> None:
    calls: list[str] = []

    async def immediate(operation):
        return await operation()

    async def fake_resolve(_url):
        calls.append("id")
        return "123456789012345"

    async def fake_dtk(_url, _config):
        calls.append("dtk")
        raise MediaDownloadError(FailureKind.AUTH, "dtk unavailable")

    async def fake_signed_guest(_aweme_id, _source_url):
        calls.append("signed_guest")
        raise MediaDownloadError(FailureKind.PARSE, "signed guest unavailable")

    async def fake_native(_aweme_id):
        calls.append("native")
        return object()

    monkeypatch.setattr(resolver, "_retry", immediate)
    monkeypatch.setattr(resolver, "resolve_aweme_id", fake_resolve)
    monkeypatch.setattr(resolver, "fetch_dtk_context", fake_dtk)
    monkeypatch.setattr(resolver, "fetch_signed_guest_context", fake_signed_guest)
    monkeypatch.setattr(resolver, "fetch_douyin_context", fake_native)

    class Config:
        douyin_api_provider = "dtk"
        douyin_api_base_url = "https://demo.douyin.wtf"

    result = asyncio.run(resolver.resolve_context("https://www.douyin.com/video/123456789012345", Config()))
    assert result[0] == "123456789012345"
    assert calls == ["id", "dtk", "signed_guest", "native"]


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


def test_media_parser_success_does_not_call_native(monkeypatch) -> None:
    calls: list[str] = []

    async def immediate(operation):
        return await operation()

    async def fake_resolve(_url):
        calls.append("id")
        return "123456789012345"

    async def fake_media_parser(_url, _config):
        calls.append("media_parser")
        return object()

    async def fake_native(_aweme_id):
        calls.append("native")
        return object()

    monkeypatch.setattr(resolver, "_retry", immediate)
    monkeypatch.setattr(resolver, "resolve_aweme_id", fake_resolve)
    monkeypatch.setattr(resolver, "fetch_media_parser_context", fake_media_parser)
    monkeypatch.setattr(resolver, "fetch_douyin_context", fake_native)

    class Config:
        douyin_api_provider = "media_parser"
        douyin_api_base_url = "http://media-parser:8051"

    asyncio.run(resolver.resolve_context("https://www.douyin.com/video/123456789012345", Config()))
    assert calls == ["id", "media_parser"]


def test_native_prefers_signed_guest_api(monkeypatch) -> None:
    calls: list[str] = []

    async def immediate(operation):
        return await operation()

    async def fake_resolve(_url):
        calls.append("id")
        return "123456789012345"

    async def fake_signed_guest(_aweme_id, source_url):
        calls.append(f"signed_guest:{source_url}")
        return object()

    async def fake_native(_aweme_id):
        calls.append("share_page")
        return object()

    monkeypatch.setattr(resolver, "_retry", immediate)
    monkeypatch.setattr(resolver, "resolve_aweme_id", fake_resolve)
    monkeypatch.setattr(resolver, "fetch_signed_guest_context", fake_signed_guest)
    monkeypatch.setattr(resolver, "fetch_douyin_context", fake_native)

    class Config:
        douyin_api_provider = "native"

    asyncio.run(resolver.resolve_context("https://www.douyin.com/video/123456789012345", Config()))
    assert calls == ["id", "signed_guest:https://www.douyin.com/video/123456789012345"]


def test_signed_guest_failure_falls_back_to_share_page(monkeypatch) -> None:
    calls: list[str] = []

    async def immediate(operation):
        return await operation()

    async def fake_resolve(_url):
        return "123456789012345"

    async def fake_signed_guest(_aweme_id, _source_url):
        calls.append("signed_guest")
        raise MediaDownloadError(FailureKind.AUTH, "guest unavailable")

    async def fake_native(_aweme_id):
        calls.append("share_page")
        return object()

    monkeypatch.setattr(resolver, "_retry", immediate)
    monkeypatch.setattr(resolver, "resolve_aweme_id", fake_resolve)
    monkeypatch.setattr(resolver, "fetch_signed_guest_context", fake_signed_guest)
    monkeypatch.setattr(resolver, "fetch_douyin_context", fake_native)

    class Config:
        douyin_api_provider = "native"

    asyncio.run(resolver.resolve_context("https://www.douyin.com/video/123456789012345", Config()))
    assert calls == ["signed_guest", "share_page"]
