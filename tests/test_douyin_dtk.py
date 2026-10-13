from __future__ import annotations

import pytest

from nekro_plugin_media.download_kit import FailureKind, MediaDownloadError
from nekro_plugin_media.douyin import dtk
from nekro_plugin_media.douyin.models import DouyinContext


class _Response:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return self.payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, url: str):
        self.calls.append(("GET", url, {}))
        return next(self.responses)

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return next(self.responses)


def test_dtk_video_response_maps_to_context() -> None:
    context = DouyinContext.from_dtk_data(
        {
            "title": "标题",
            "description": "描述",
            "author": {"nickname": "作者"},
            "duration_ms": 12_500,
            "media": {"video": {"url": "https://cdn.example/video.mp4", "urls": []}},
            "stats": {"digg_count": 8, "comment_count": 2},
        }
    )
    assert context.desc == "描述"
    assert context.author == "作者"
    assert context.duration == 12
    assert context.video_url == "https://cdn.example/video.mp4"
    assert context.digg_count == 8
    assert context.comment_count == 2
    assert not context.is_gallery


def test_dtk_gallery_response_maps_image_urls() -> None:
    context = DouyinContext.from_dtk_data(
        {
            "media": {"images": [{"url": "https://cdn.example/1.jpg"}, {"urls": ["https://cdn.example/2.jpg"]}]},
            "description": "图集",
        }
    )
    assert context.is_gallery
    assert context.image_urls == ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"]


def test_dtk_video_urls_fall_back_when_primary_url_missing() -> None:
    context = DouyinContext.from_dtk_data(
        {"media": {"video": {"urls": ["https://cdn.example/fallback.mp4"]}}}
    )
    assert context.video_url == "https://cdn.example/fallback.mp4"


def test_video_takes_precedence_when_response_also_has_images() -> None:
    context = DouyinContext.from_dtk_data(
        {
            "media": {
                "video": {"url": "https://cdn.example/video.mp4"},
                "images": [{"url": "https://cdn.example/cover.jpg"}],
            }
        }
    )
    assert context.video_url is not None
    assert context.image_urls
    assert not context.is_gallery


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/x", "http://localhost/x", "http://169.254.169.254/x"])
def test_dtk_rejects_non_public_media_urls(url: str) -> None:
    context = DouyinContext.from_dtk_data({"media": {"video": {"url": url}}})
    assert context.video_url is None


def test_dtk_response_error_classification() -> None:
    assert dtk._failure_kind_for_response(401, {}) is FailureKind.AUTH
    assert dtk._failure_kind_for_response(503, {}) is FailureKind.SERVER
    assert dtk._failure_kind_for_response(400, {}) is FailureKind.PARSE
    assert dtk._failure_kind_for_response(200, {"success": True}) is None


def test_dtk_payload_error_raises_without_exposing_message() -> None:
    with pytest.raises(MediaDownloadError, match="UNAUTHENTICATED") as caught:
        dtk._raise_for_response(200, {"success": False, "error": {"code": "UNAUTHENTICATED", "message": "secret"}})
    assert caught.value.kind is FailureKind.AUTH
    assert "secret" not in str(caught.value)


def test_dtk_is_opt_in() -> None:
    class Config:
        douyin_api_provider = "native"
        douyin_api_base_url = "https://demo.douyin.wtf"

    assert not dtk.is_dtk_enabled(Config())
    Config.douyin_api_provider = "dtk"
    assert dtk.is_dtk_enabled(Config())


def test_fetch_dtk_context_authenticates_demo_and_maps_response(monkeypatch) -> None:
    session = _Session(
        [
            _Response({"success": True, "data": {"username": "demo", "password": "not-logged"}}),
            _Response({"success": True}),
            _Response(
                {
                    "success": True,
                    "data": {
                        "title": "标题",
                        "author": "作者",
                        "duration_ms": 2_000,
                        "media": {"video": {"url": "https://cdn.example/video.mp4"}},
                        "stats": {"digg_count": 3},
                    },
                }
            ),
        ]
    )
    monkeypatch.setattr(dtk.aiohttp, "ClientSession", lambda **_kwargs: session)
    monkeypatch.setattr(dtk, "_validate_public_media_hosts", _skip_host_validation)

    class Config:
        douyin_api_base_url = "https://api.example"
        douyin_api_key = ""
        douyin_api_demo_login = True
        douyin_api_timeout = 45

    import asyncio

    context = asyncio.run(dtk.fetch_dtk_context("https://www.douyin.com/video/123456789012345", Config()))
    assert context.author == "作者"
    assert context.video_url == "https://cdn.example/video.mp4"
    assert [call[0] for call in session.calls] == ["GET", "POST", "POST"]
    assert session.calls[-1][2]["json"]["url"].startswith("https://www.douyin.com/")


async def _skip_host_validation(_context):
    return None


@pytest.mark.parametrize(
    ("payload", "status", "kind"),
    [
        ({"success": False, "error": {"code": "UNAUTHENTICATED", "message": "private"}}, 200, FailureKind.AUTH),
        ({"success": False, "error": {"code": "BAD_URL", "message": "private"}}, 200, FailureKind.PARSE),
    ],
)
def test_fetch_dtk_context_maps_service_failures(monkeypatch, payload, status, kind) -> None:
    session = _Session([_Response(payload, status)])
    monkeypatch.setattr(dtk.aiohttp, "ClientSession", lambda **_kwargs: session)

    class Config:
        douyin_api_base_url = "https://api.example"
        douyin_api_key = "configured-key"
        douyin_api_demo_login = False
        douyin_api_timeout = 5

    import asyncio

    with pytest.raises(MediaDownloadError) as caught:
        asyncio.run(dtk.fetch_dtk_context("https://www.douyin.com/video/123456789012345", Config()))
    assert caught.value.kind is kind
