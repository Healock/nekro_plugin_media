"""抖音分享页解析的纯逻辑单元测试。

`douyin/models.py` 不依赖网络与机器人框架，这里直接从文件加载该模块，
覆盖 `_ROUTER_DATA` 提取、去水印地址转换、图集判定与字段映射等易错逻辑。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODELS_PATH = Path(__file__).resolve().parents[1] / "douyin" / "models.py"


def _load_models():
    spec = importlib.util.spec_from_file_location("_douyin_models_under_test", MODELS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


models = _load_models()


def test_normalize_play_url_strips_watermark() -> None:
    watermark = "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0200fg10000"
    assert models.normalize_play_url(watermark) == "https://aweme.snssdk.com/aweme/v1/play/?video_id=v0200fg10000"


def test_extract_aweme_id_from_various_links() -> None:
    assert models.extract_aweme_id("https://www.douyin.com/video/7521023890996514083") == "7521023890996514083"
    assert models.extract_aweme_id("https://www.iesdouyin.com/share/note/7469411074119322899/") == "7469411074119322899"
    assert models.extract_aweme_id("https://v.douyin.com/iRxAbCd") is None


def test_extract_router_data_handles_braces_inside_strings() -> None:
    html = (
        "<html><script>window._ROUTER_DATA = "
        '{"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": '
        '[{"desc": "a } brace inside string", "create_time": 1700000000}]}}}}'
        "</script></html>"
    )
    data = models.extract_router_data(html)
    assert data is not None
    assert models.find_item(data)["desc"] == "a } brace inside string"


def test_extract_router_data_returns_none_without_marker() -> None:
    assert models.extract_router_data("<html>no router data</html>") is None


def _video_item() -> dict:
    return {
        "desc": "测试视频文案\n第二行",
        "create_time": 1_700_000_000,
        "statistics": {"digg_count": 12, "comment_count": 3},
        "author": {"nickname": "测试作者", "avatar_thumb": {"url_list": ["https://img/avatar.jpg"]}},
        "video": {
            "duration": 15_000,
            "play_addr": {"uri": "v0200", "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0200"]},
            "cover": {"url_list": ["https://img/cover.jpg"]},
        },
    }


def test_video_item_is_parsed_without_watermark() -> None:
    context = models.DouyinContext.from_item(_video_item())
    assert context.author == "测试作者"
    assert context.avatar_url == "https://img/avatar.jpg"
    assert context.video_url == "https://aweme.snssdk.com/aweme/v1/play/?video_id=v0200"
    assert context.cover_url == "https://img/cover.jpg"
    assert context.duration == 15  # 毫秒 -> 秒
    assert context.digg_count == 12
    assert not context.is_gallery


def test_video_item_exposes_watermarked_fallback_url() -> None:
    context = models.DouyinContext.from_item(_video_item())
    # 去水印地址用于主档位，原始带水印地址保留为最后降级档位。
    assert context.video_url_watermarked == "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0200"
    assert context.video_url == "https://aweme.snssdk.com/aweme/v1/play/?video_id=v0200"


def test_from_router_data_locates_item() -> None:
    router = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [_video_item()]}}}}
    context = models.DouyinContext.from_router_data(router)
    assert context is not None
    assert context.author == "测试作者"


def test_gallery_item_collects_images() -> None:
    item = {
        "desc": "图集",
        "author": {"nickname": "作者"},
        "images": [
            {"url_list": ["https://img/1.jpg", "https://img/1b.jpg"]},
            {"url_list": ["https://img/2.jpg"]},
        ],
    }
    context = models.DouyinContext.from_item(item)
    assert context.is_gallery
    assert context.image_urls == ["https://img/1.jpg", "https://img/2.jpg"]
    assert context.video_url is None


def test_play_url_falls_back_to_uri_when_url_list_missing() -> None:
    context = models.DouyinContext.from_item({"video": {"play_addr": {"uri": "v0300", "url_list": []}}})
    assert context.video_url == "https://aweme.snssdk.com/aweme/v1/play/?video_id=v0300&ratio=720p"


def test_title_truncation() -> None:
    context = models.DouyinContext(desc="x" * 200)
    assert len(context.title(10)) == 12  # 10 字符 + 省略号
    assert context.title(10).endswith("……")
    assert models.DouyinContext(desc="  ").title(10) == "无标题"
