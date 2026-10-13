from __future__ import annotations

from nekro_plugin_media.douyin.models import DouyinContext


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
