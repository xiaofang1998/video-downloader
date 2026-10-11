"""X / Twitter 兜底解析的测试。

背景：X 对受限推文（需登录查看 / 带年龄门槛）不给游客态下发 media，yt-dlp 报
"No video could be found in this tweet"，而媒体本身是公开可下的（镜像接口给直链）。
所以这一层必须稳：正常推文仍走 yt-dlp，只有失败时才用它兜底。
"""
from __future__ import annotations

import json

import pytest

from xydl import twitter

#: fxtwitter v2 的真实结构（字段裁剪自实测响应，文案已替换为中性示例）
FX_PAYLOAD = {
    "code": 200, "message": "OK",
    "tweet": {
        "text": "New demo clip is up — building a character step by step",
        "media": {
            "videos": [
                {"url": "https://video.twimg.com/amplify_video/1/vid/avc1/720x1280/low.mp4",
                 "thumbnail_url": "https://pbs.twimg.com/a.jpg",
                 "duration": 113, "width": 720, "height": 1280},
                {"url": "https://video.twimg.com/amplify_video/1/vid/avc1/1414x720/best.mp4",
                 "duration": 113.0, "size": {"width": 1414, "height": 720}},
            ],
            "photos": [{"url": "https://pbs.twimg.com/media/p.jpg",
                        "width": 1200, "height": 800}],
        },
    },
}

#: vxtwitter 的扁平结构
VX_PAYLOAD = {
    "text": "hello",
    "mediaURLs": ["https://video.twimg.com/ext_tw_video/2/vid/1280x720/a.mp4"],
    "media_extended": [
        {"type": "video", "url": "https://video.twimg.com/ext_tw_video/2/vid/1280x720/a.mp4",
         "thumbnail_url": "https://pbs.twimg.com/b.jpg",
         "size": {"width": 1280, "height": 720}},
    ],
}


@pytest.mark.parametrize("url,expected", [
    ("https://x.com/example_user/status/1234567890123456789?s=20",
     ("example_user", "1234567890123456789")),
    ("https://twitter.com/i/status/1732824684683784516", ("", "1732824684683784516")),
    ("https://mobile.twitter.com/foo/statuses/123456789", ("foo", "123456789")),
    ("https://x.com/foo", None),
    ("https://example.com/status/123456789", None),
])
def test_parse_status(url, expected):
    assert twitter.parse_status(url) == expected


@pytest.mark.parametrize("url,ok", [
    ("https://x.com/a/status/1", True),
    ("https://twitter.com/a/status/1", True),
    ("https://www.bilibili.com/video/BV1xx", False),
    ("https://youtube.com/watch?v=1", False),
])
def test_is_twitter(url, ok):
    assert twitter.is_twitter(url) is ok


def test_extract_media_fx_shape_sorts_by_resolution():
    """fxtwitter 结构：视频排前、按分辨率从高到低（上层取第一条就最好）。"""
    items = twitter.extract_media(FX_PAYLOAD)
    kinds = [item["kind"] for item in items]
    assert kinds[0] == "video" and "photo" in kinds
    videos = [item for item in items if item["kind"] == "video"]
    assert videos[0]["url"].endswith("best.mp4"), "高分辨率要排第一"
    assert videos[0]["width"] == 1414 and videos[0]["height"] == 720
    assert videos[0]["duration"] == 113


def test_extract_media_vx_shape():
    """vxtwitter 结构（mediaURLs + media_extended）也要认，不能只支持一种。"""
    items = twitter.extract_media(VX_PAYLOAD)
    videos = [item for item in items if item["kind"] == "video"]
    assert len(videos) == 1, f"重复的直链要按 url 去重：{items}"
    assert videos[0]["width"] == 1280 and videos[0]["thumbnail"].endswith("b.jpg")


def test_extract_media_tolerates_garbage():
    """镜像接口随时会变：字段缺失/类型不对都不能抛异常。"""
    for payload in ({}, [], None, {"tweet": None}, {"tweet": {"media": []}},
                    {"tweet": {"media": {"videos": [None, {"url": 123}]}}}):
        assert twitter.extract_media(payload) == []


def test_fetch_media_uses_mirror_and_returns_caption():
    seen: list[str] = []

    def fake_fetch(url: str) -> str:
        seen.append(url)
        return json.dumps(FX_PAYLOAD)

    items, caption, err = twitter.fetch_media(
        "https://x.com/example_user/status/1234567890123456789", fetch=fake_fetch)
    assert err == ""
    assert items and items[0]["kind"] == "video"
    assert "demo clip" in caption
    assert seen and "fxtwitter" in seen[0]


def test_fetch_media_falls_back_to_second_mirror():
    """第一个镜像挂了要自动试第二个（镜像都是第三方，随时可能 403/超时）。"""
    calls: list[str] = []

    def fake_fetch(url: str) -> str:
        calls.append(url)
        if "fxtwitter" in url:
            raise OSError("boom")
        return json.dumps(VX_PAYLOAD)

    items, _caption, err = twitter.fetch_media(
        "https://x.com/foo/status/2109142587022229998", fetch=fake_fetch)
    assert err == "" and items
    assert len(calls) == 2 and "vxtwitter" in calls[1]


def test_fetch_media_reports_why_when_no_media():
    def fake_fetch(url: str) -> str:
        return json.dumps({"code": 404, "message": "Tweet not found"})

    items, _caption, err = twitter.fetch_media(
        "https://x.com/foo/status/2109142587022229998", fetch=fake_fetch)
    assert items == []
    assert "没有媒体" in err or "不存在" in err or "code=404" in err


def test_fetch_media_without_id_is_a_clear_error():
    items, _caption, err = twitter.fetch_media("https://x.com/foo", fetch=lambda _u: "{}")
    assert items == []
    assert "推文 ID" in err
