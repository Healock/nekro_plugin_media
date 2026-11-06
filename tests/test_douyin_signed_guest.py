from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from nekro_plugin_media.download_kit import FailureKind, MediaDownloadError
from nekro_plugin_media.douyin import signed_guest


def test_websign_matches_known_fixture() -> None:
    signed = signed_guest.sign(
        "aweme_id=123&uifid=guest&a_bogus=A%2BB%2F%3D",
        "guest",
        timestamp=1_700_000_000,
    )
    assert signed.timestamp == "1700000000"
    assert signed.signature == "d6ffd103bb2de29134a9e15755c2d5c4"
    assert signed.query.endswith(
        "&x-secsdk-web-signature=d6ffd103bb2de29134a9e15755c2d5c4"
    )


def test_guest_request_has_required_signatures_and_safe_referer(monkeypatch) -> None:
    monkeypatch.setattr(
        signed_guest.BrowserFingerprintGenerator,
        "generate_fingerprint",
        lambda _browser: "stable-test-fingerprint",
    )
    monkeypatch.setattr(
        signed_guest.ABogus,
        "generate_abogus",
        lambda _self, _params, _body: ("", "test-a-bogus", "", ""),
    )
    request = signed_guest.build_guest_request(
        "123456789012345",
        signed_guest.GuestSession("guest-ttwid", "guest-uifid"),
        "https://www.douyin.com/video/123456789012345",
        timestamp=1_700_000_000,
    )
    params = parse_qs(urlparse(request.url).query)
    assert params["aweme_id"] == ["123456789012345"]
    assert params["uifid"] == ["guest-uifid"]
    assert params["a_bogus"] == ["test-a-bogus"]
    assert params["timestamp"] == ["1700000000"]
    assert request.headers["Referer"] == "https://www.douyin.com/video/123456789012345"
    assert request.headers["x-secsdk-web-signature"] == params[
        "x-secsdk-web-signature"
    ][0]
    assert request.headers["x-secsdk-web-expire"] == "1700000000"


def test_context_keeps_guest_cookie_out_of_serialized_data() -> None:
    session = signed_guest.GuestSession("guest-ttwid", "guest-uifid")
    context = signed_guest._context_from_detail(
        {
            "desc": "测试视频",
            "author": {"nickname": "作者"},
            "video": {
                "play_addr": {
                    "url_list": ["https://v26-web.douyinvod.com/video.mp4"]
                },
                "duration": 12_000,
            },
        },
        session,
    )
    assert context.video_url == "https://v26-web.douyinvod.com/video.mp4"
    assert context.download_cookie == "ttwid=guest-ttwid; UIFID_TEMP=guest-uifid;"
    assert "guest-ttwid" not in context.model_dump_json()
    assert "guest-uifid" not in context.model_dump_json()


def test_context_rejects_unsafe_media_urls() -> None:
    session = signed_guest.GuestSession("guest-ttwid", "guest-uifid")
    with pytest.raises(MediaDownloadError, match="无效媒体地址") as captured:
        signed_guest._context_from_detail(
            {
                "video": {
                    "play_addr": {"url_list": ["http://127.0.0.1/video.mp4"]}
                }
            },
            session,
        )
    assert captured.value.kind is FailureKind.PARSE


@pytest.mark.asyncio
async def test_fetch_context_refreshes_guest_identity_and_passes_cookie(monkeypatch) -> None:
    api = signed_guest.DouyinSignedGuestAPI()
    sessions = [
        signed_guest.GuestSession("old-ttwid", "old-uifid"),
        signed_guest.GuestSession("new-ttwid", "new-uifid"),
    ]
    session_calls: list[bool] = []
    request_headers: list[dict[str, str]] = []

    async def get_session(*, refresh: bool = False):
        session_calls.append(refresh)
        return sessions[1 if refresh else 0]

    monkeypatch.setattr(api, "_get_session", get_session)
    monkeypatch.setattr(signed_guest, "ensure_dependency", lambda: None)
    monkeypatch.setattr(
        signed_guest,
        "build_guest_request",
        lambda _aweme_id, _session, _source_url: signed_guest.GuestRequest(
            "https://www.douyin.com/detail", {}
        ),
    )

    payloads = iter(
        [
            {"status_code": 1},
            {
                "aweme_detail": {
                    "video": {
                        "play_addr": {
                            "url_list": ["https://v26-web.douyinvod.com/video.mp4"]
                        }
                    }
                }
            },
        ]
    )

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self, **_kwargs):
            return next(payloads)

    class FakeClientSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, _url, *, headers):
            request_headers.append(headers)
            return FakeResponse()

    monkeypatch.setattr(signed_guest.aiohttp, "ClientSession", FakeClientSession)

    context = await api.fetch_context("123456789012345")

    assert session_calls == [False, True]
    assert request_headers[0]["Cookie"] == "ttwid=old-ttwid; UIFID_TEMP=old-uifid;"
    assert request_headers[1]["Cookie"] == "ttwid=new-ttwid; UIFID_TEMP=new-uifid;"
    assert context.download_cookie == request_headers[1]["Cookie"]
