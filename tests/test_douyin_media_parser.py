from nekro_plugin_media.douyin.media_parser import _failure_kind
from nekro_plugin_media.douyin.models import DouyinContext


def test_media_parser_data_maps_video() -> None:
    context = DouyinContext.from_media_parser_data(
        {
            "title": "测试标题",
            "author": {"nickname": "作者"},
            "video_url": "https://cdn.example/video.mp4",
            "cover_url": "https://cdn.example/cover.jpg",
            "duration": 12,
        }
    )
    assert context.desc == "测试标题"
    assert context.author == "作者"
    assert context.video_url == "https://cdn.example/video.mp4"
    assert context.cover_url == "https://cdn.example/cover.jpg"
    assert context.duration == 12


def test_media_parser_data_maps_gallery() -> None:
    context = DouyinContext.from_media_parser_data(
        {"desc": "图集", "image_list": ["https://cdn.example/1.jpg", {"url": "https://cdn.example/2.jpg"}]}
    )
    assert context.is_gallery
    assert context.image_urls == ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"]


def test_media_parser_response_statuses() -> None:
    assert _failure_kind(200, {"code": 200, "succ": True}) is None
    assert _failure_kind(401, {}) is not None
    assert _failure_kind(200, {"code": 400, "succ": False}) is not None
