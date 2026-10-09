"""TikHub 第三方解析测试（mock 网络，离线可跑）。"""

from __future__ import annotations

from xydl import tikhub


class _Resp:
    def __init__(self, payload: dict):
        self._p = payload

    def json(self):
        return self._p


def test_resolve_requires_api_key():
    url, err = tikhub.resolve("https://www.douyin.com/video/123", "")
    assert url == ""
    assert err  # 有错误说明


def test_resolve_extracts_play_addr(monkeypatch):
    captured = {}

    def _req(method, url, *, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Resp({
            "code": 200,
            "data": {"aweme_detail": {
                "desc": "测试标题",
                "video": {"play_addr_265": {"url_list": ["https://cdn/dy.mp4"]}},
            }},
        })

    monkeypatch.setattr(tikhub, "request", _req)
    url, title = tikhub.resolve("https://www.douyin.com/video/123", "key123")
    assert url == "https://cdn/dy.mp4"
    assert title == "测试标题"
    assert "douyin" in captured["url"]
    assert captured["params"]["share_url"] == "https://www.douyin.com/video/123"
    assert captured["headers"]["Authorization"] == "Bearer key123"


def test_resolve_falls_back_to_play_addr(monkeypatch):
    monkeypatch.setattr(tikhub, "request", lambda *a, **k: _Resp({
        "code": 200,
        "data": {"aweme_detail": {
            "desc": "t",
            "video": {"play_addr": {"url_list": ["https://cdn/h264.mp4"]}},
        }},
    }))
    url, _ = tikhub.resolve("https://www.tiktok.com/@x/video/1", "k")
    assert url == "https://cdn/h264.mp4"


def test_resolve_error_code(monkeypatch):
    monkeypatch.setattr(tikhub, "request",
                        lambda *a, **k: _Resp({"code": 403, "message": "额度用完"}))
    url, err = tikhub.resolve("https://www.douyin.com/video/123", "k")
    assert url == ""
    assert "403" in err or "额度" in err


def test_resolve_kuaishou_main_mv_urls(monkeypatch):
    """快手走 data.photos[0].main_mv_urls[0].url。"""
    monkeypatch.setattr(tikhub, "request", lambda *a, **k: _Resp({
        "code": 200,
        "data": {"photos": [{
            "caption": "立定跳远",
            "main_mv_urls": [{"url": "http://v1.kwaicdn.com/xxx.mp4"}],
        }]},
    }))
    url, title = tikhub.resolve("https://v.kuaishou.com/abc", "k")
    assert url == "http://v1.kwaicdn.com/xxx.mp4"
    assert title == "立定跳远"


def test_resolve_kuaishou_platform_detected(monkeypatch):
    captured = {}

    def _req(method, url, *, params=None, headers=None, timeout=None):
        captured["url"] = url
        return _Resp({"code": 200, "data": {"photos": []}})

    monkeypatch.setattr(tikhub, "request", _req)
    tikhub.resolve("https://www.kuaishou.com/short-video/3xabc", "k")
    assert "kuaishou" in captured["url"]
