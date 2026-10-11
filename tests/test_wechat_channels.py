"""微信视频号解析层测试。

默认**全部离线**：网络那一层用桩替换掉，保证 CI 结果确定。
真实接口的活体测试用 ``XYDL_LIVE_TESTS=1`` 显式打开（见文件末尾）。
"""

from __future__ import annotations

import json
import os

import pytest

from xydl import wechat_channels as wc
from xydl.config import Config
from xydl.http import Response

SHARE_URL = "https://weixin.qq.com/sph/AJizVCRhAt"
PREVIEW_URL = "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AJizVCRhAt"
EID = "export/UzFfBgAAxMOjaFVFf3ztjczT4DCsIumMPwYsDui5zz0hkxXRlI9ld7hWmQ"
WEB_URL = f"https://channels.weixin.qq.com/web/pages/feed?eid={EID}"


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config({"net": {"proxy": "none"},
                "paths": {"data_dir": str(tmp_path / "d")}})
    # 元宝入口默认会真的开一个浏览器（那是它的工作方式），单元测试里必须关掉，
    # 否则每个短链用例都去弹窗。要测元宝的用例自己显式打开。
    c.set("download.wechat_channels.yuanbao_enabled", False)
    return c


@pytest.fixture(autouse=True)
def _isolate_feed_cache():
    """匿名元数据是带 TTL 的模块级缓存，测试之间必须隔离。

    不隔离的话，前一个用例缓存的"成功响应"会被后一个用例拿到，
    后者精心准备的桩（错误信封/超时/无视频字段）就永远不生效 ——
    这种"测试通过但没测到东西"的假绿比失败更危险。
    """
    wc.clear_feed_cache()
    yield
    wc.clear_feed_cache()


# ── 链接识别 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [SHARE_URL, PREVIEW_URL, WEB_URL,
                                 "https://channels.weixin.qq.com/web/pages/home"])
def test_is_channels_url_true(url):
    assert wc.is_channels_url(url)


@pytest.mark.parametrize("url", [
    "https://mp.weixin.qq.com/s/abcdefg",          # 公众号文章，不是视频号
    "https://weixin.qq.com/",                      # 首页，没有 /sph/
    "https://www.bilibili.com/video/BV1xx411c7mD",
    "",
])
def test_is_channels_url_false(url):
    assert not wc.is_channels_url(url)


def test_parse_ref_from_share_short_link():
    ref = wc.parse_ref(SHARE_URL)
    assert ref.ok
    assert ref.short_uri == "AJizVCRhAt"
    assert ref.channel_id == "sphAJizVCRhAt"
    assert ref.share_url == SHARE_URL
    assert ref.preview_url == PREVIEW_URL


def test_parse_ref_from_preview_page():
    ref = wc.parse_ref(PREVIEW_URL)
    assert ref.short_uri == "AJizVCRhAt"


def test_parse_ref_from_web_feed_page():
    ref = wc.parse_ref(WEB_URL)
    assert ref.export_id == EID, ref.export_id
    assert ref.short_uri == ""
    assert "eid=export%2F" in ref.web_feed_url, ref.web_feed_url


def test_parse_ref_accepts_bare_code():
    assert wc.parse_ref("AJizVCRhAt").short_uri == "AJizVCRhAt"


def test_parse_ref_rejects_garbage():
    ref = wc.parse_ref("https://example.com/nothing")
    assert not ref.ok
    assert ref.error


# ── 剧集识别 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("title,expected", [
    ("出狱后，我举世无双-第1集", 1),
    ("出狱后，我举世无双-第12集", 12),
    ("某某短剧 EP12", 12),
    ("某某短剧 P3", 3),
    ("某某短剧（7）", 7),
    ("某某短剧 8", 8),
    ("第3话", 3),
])
def test_series_episode_number(title, expected):
    assert wc.series_episode_number(title) == expected


@pytest.mark.parametrize("title", ["随手拍的一天", "", "2024年总结"])
def test_series_episode_number_none(title):
    assert wc.series_episode_number(title) is None


@pytest.mark.parametrize("title,expected", [
    ("出狱后，我举世无双-第1集", "出狱后，我举世无双"),
    ("某某短剧 EP12", "某某短剧"),
    ("某某短剧（7）", "某某短剧"),
    ("随手拍的一天", "随手拍的一天"),
])
def test_series_prefix(title, expected):
    assert wc.series_prefix(title) == expected


# ── 元数据解析 ────────────────────────────────────────────────────────
PREVIEW_BODY = {
    "data": {
        "authorInfo": {"nickname": "邻里剧场"},
        "feedInfo": {
            "description": "出狱后，我举世无双-第1集",
            "coverUrl": "https://finder.video.qq.com/251/20350/stodownload?x=1",
            "likeCountFmt": "341",
            "createtime": 1770083400,
        },
        "errMsg": {"type": 0},
        "sceneInfo": {"dynamicExportId": "export/UzFfBgAAxMOjaFVFf3ztjczT4DCs"},
    },
    "errCode": 0,
    "errMsg": "",
}


class _StubRequest:
    """记录请求并按脚本返回响应。"""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        payload = self.payloads.pop(0) if self.payloads else {}
        if isinstance(payload, Response):
            return payload
        return Response(status=201, url=url, headers={"content-type": "application/json"},
                        body=json.dumps(payload).encode("utf-8"))


def test_fetch_feed_info_success(monkeypatch, cfg):
    stub = _StubRequest([PREVIEW_BODY])
    monkeypatch.setattr(wc, "request", stub)

    feed = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)
    assert feed.ok
    assert feed.title == "出狱后，我举世无双-第1集"
    assert feed.author == "邻里剧场"
    assert feed.export_id.startswith("export/")
    assert feed.playable is False

    call = stub.calls[0]
    assert call["method"] == "POST"
    assert call["json_body"] == {"baseReq": {"generalToken": ""},
                                 "shortUri": "AJizVCRhAt"}
    headers = call["headers"]
    # Origin 与 Referer 缺一不可（缺了服务端会 401 permission verification failed）
    assert headers["Origin"] == wc.API_ORIGIN
    assert headers["Referer"] == wc.API_REFERER
    assert headers["Content-Type"] == "application/json"


def test_fetch_feed_info_uses_export_id_when_no_short_uri(monkeypatch, cfg):
    stub = _StubRequest([PREVIEW_BODY])
    monkeypatch.setattr(wc, "request", stub)
    wc.fetch_feed_info(wc.parse_ref(WEB_URL), cfg)
    assert stub.calls[0]["json_body"]["exportId"] == EID


def test_fetch_feed_info_handles_error_envelope(monkeypatch, cfg):
    monkeypatch.setattr(wc, "request", _StubRequest([
        {"error": {"name": "ServerError", "message": "请求异常"}}]))
    feed = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)
    assert not feed.ok
    assert "请求异常" in feed.error


def test_fetch_feed_info_handles_permission_failure(monkeypatch, cfg):
    monkeypatch.setattr(wc, "request", _StubRequest([
        {"errCode": -1, "errMsg": "permission verification failed", "error": None}]))
    feed = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)
    assert not feed.ok
    assert "permission" in feed.error


def test_fetch_feed_info_handles_network_error(monkeypatch, cfg):
    monkeypatch.setattr(wc, "request", _StubRequest([
        Response(status=0, url=SHARE_URL, error="请求超时（5s）")]))
    feed = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)
    assert not feed.ok
    assert "超时" in feed.error


def test_fetch_feed_info_marks_unplayable(monkeypatch, cfg):
    """预览场景不可用的内容**不能算成功** —— 否则 wxinfo 会打出误导性的 ✓。"""
    body = json.loads(json.dumps(PREVIEW_BODY))
    body["data"]["errMsg"] = {"type": 2}
    monkeypatch.setattr(wc, "request", _StubRequest([body]))
    feed = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)
    assert not feed.ok
    assert "errMsg.type=2" in feed.error


def test_fetch_feed_info_picks_up_video_when_present(monkeypatch, cfg):
    """不同场景下匿名响应偶尔**会**带 videoUrl —— 带了就直接用，省一次解析。"""
    body = json.loads(json.dumps(PREVIEW_BODY))
    body["data"]["feedInfo"]["h264VideoInfo"] = {
        "videoUrl": "https://finder.video.qq.com/251/20302/stodownload?a=1",
        "decodeKey": "999",
    }
    monkeypatch.setattr(wc, "request", _StubRequest([body]))
    feed = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)
    assert feed.playable
    assert feed.video_url.endswith("a=1")
    assert feed.decode_key == "999"


def test_media_from_feed_dict_shapes():
    assert wc._media_from_feed_dict({}) == ("", "")
    # h264 优先：**故意和页面播放器相反**。页面自己播怎么清晰怎么来（h265），
    # 但我们是"交付给客户"，h265 在老手机/剪映上打不开，所以取兼容性更好的 h264，
    # 与 download.prefer_compatible 的取向一致。
    assert wc._media_from_feed_dict(
        {"h265VideoInfo": {"videoUrl": "u265"}, "h264VideoInfo": {"videoUrl": "u264"}}
    )[0] == "u264", "交付优先兼容性：选 h264"
    assert wc._media_from_feed_dict({"videoUrl": "plain"})[0] == "plain"
    assert wc._media_from_feed_dict(
        {"media": [{"url": "m1", "decode_key": "k"}]}) == ("m1", "k")


# ── TikHub ────────────────────────────────────────────────────────────
#: 取自 TikHub ``fetch_video_detail`` 的真实响应结构（只保留相关字段）
TIKHUB_DETAIL = {
    "id": "14396973035218999573",
    "nickname": "某某作者",
    "username": "v2_060000231003b20faec8c6e4811dc1d4c602ee30b0771bbcf220c67926bb76ab7702ac335a53@finder",
    "createtime": 1760000000,
    "object_nonce_id": "123",
    "object_desc": {
        "description": "出狱后，我举世无双-第1集",
        "media_type": 4,
        "media": [{
            "url": "https://finder.video.qq.com/251/20302/stodownload?encfilekey=x",
            "full_url": "",
            "decode_key": "2136343393",
            "file_size": 14088528,
            "cover_url": "https://wxapp.tc.qq.com/cover",
            "width": 720.0,
            "height": 1280.0,
            "codec_info": {"coding_format": "h265"},
        }],
    },
}


def test_parse_tikhub_media():
    url, key, meta = wc.parse_tikhub_media(TIKHUB_DETAIL)
    assert url.endswith("encfilekey=x")
    assert key == "2136343393", "decode_key 必须与同一次响应的 url 配对返回"
    assert meta["title"] == "出狱后，我举世无双-第1集"
    assert meta["author"] == "某某作者"
    assert meta["file_size"] == 14088528
    assert meta["username"].startswith("v2_")


def test_parse_tikhub_media_prefers_h264_for_compatibility():
    data = json.loads(json.dumps(TIKHUB_DETAIL))
    data["object_desc"]["media"] = [
        {"url": "u265", "codec_info": {"coding_format": "h265"}},
        {"url": "u264", "codec_info": {"coding_format": "h264"}},
    ]
    assert wc.parse_tikhub_media(data)[0] == "u264"


def test_parse_tikhub_media_empty():
    url, key, meta = wc.parse_tikhub_media({})
    assert (url, key) == ("", "")
    assert isinstance(meta, dict)
    assert wc.parse_tikhub_media({"object_desc": {"media": []}})[0] == ""


def test_tikhub_request_errors_are_described(monkeypatch, cfg):
    monkeypatch.setattr(wc, "request", _StubRequest([
        {"code": 400, "message": "bad request", "data": None}]))
    data, error = wc.tikhub_video_detail(wc.parse_ref(SHARE_URL), "key")
    assert not data
    assert "bad request" in error


# ── 解析门面 ──────────────────────────────────────────────────────────
def test_resolve_media_uses_feed_direct_url(monkeypatch, cfg):
    monkeypatch.setattr(wc, "request", _StubRequest([PREVIEW_BODY]))
    feed = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)
    feed.video_url = "https://finder.video.qq.com/v.mp4"
    media = wc.resolve_media(SHARE_URL, cfg, feed=feed, allow_browser=False)
    assert media.ok and media.via == "feed"


def test_resolve_media_uses_tikhub_when_configured(monkeypatch, cfg):
    cfg.set("delivery.tikhub.api_key", "test-key")
    stub = _StubRequest([PREVIEW_BODY, {"code": 200, "data": TIKHUB_DETAIL}])
    monkeypatch.setattr(wc, "request", stub)

    media = wc.resolve_media(SHARE_URL, cfg, allow_browser=False)
    assert media.ok
    assert media.via == "tikhub"
    assert media.decode_key == "2136343393"
    # 第二次调用必须打的是 TikHub，而不是再问一次预览接口
    assert "wechat_channels" in stub.calls[1]["url"]


def test_resolve_media_reports_actionable_error(monkeypatch, cfg):
    """取不到媒体时，报错必须告诉客户**下一步该做什么**。

    产品流程：视频号短链要先过一遍腾讯元宝，拿它解析出的带 token 的链接粘回来。
    所以提示里必须出现"元宝"和"粘回"，不能只丢一句技术报错。
    """
    cfg.set("download.wechat_channels.use_browser", False)
    monkeypatch.setattr(wc, "request", _StubRequest([PREVIEW_BODY]))
    media = wc.resolve_media(SHARE_URL, cfg, allow_browser=False)
    assert not media.ok
    assert "元宝" in media.error, "要引导客户去元宝解析"
    assert "粘贴" in media.error or "粘回" in media.error
    assert "token" in media.error


def test_browser_capture_is_off_by_default(cfg):
    """浏览器抓取对视频号**实测无效**，默认必须是关的。

    回归意义：这个开关一旦被改回 True，每个视频号订单都会白等 25 秒 + 白弹一个
    浏览器窗口，而且仍然拿不到 mp4。
    """
    assert cfg.get("download.wechat_channels.use_browser") is False
    media = wc.resolve_media(SHARE_URL, cfg, feed=wc.ChannelsFeed(ok=True),
                             allow_browser=False)
    assert not media.ok


def test_resolve_media_passes_browser_wait_sec(monkeypatch, cfg):
    """配置里的 browser_wait_sec 必须真的传到抓取函数（否则就是个装饰品）。"""
    cfg.set("download.wechat_channels.use_browser", True)
    cfg.set("download.wechat_channels.browser_wait_sec", 42)
    monkeypatch.setattr(wc, "request", _StubRequest([PREVIEW_BODY]))
    seen: dict[str, object] = {}

    def fake_capture(ref, config, *, wait_sec, log):
        seen["wait_sec"] = wait_sec
        capture = wc.BrowserCapture()
        capture.video_url = "https://finder.video.qq.com/v.mp4"
        capture.decode_key = "123"
        return capture

    monkeypatch.setattr(wc, "capture_via_browser", fake_capture)
    media = wc.resolve_media(SHARE_URL, cfg)
    assert media.ok and media.via == "browser"
    assert seen["wait_sec"] == 42.0


def test_resolve_media_disabled(cfg):
    cfg.set("download.wechat_channels.enabled", False)
    media = wc.resolve_media(SHARE_URL, cfg, allow_browser=False)
    assert not media.ok and "关闭" in media.error


def test_resolve_media_bad_ref(cfg):
    media = wc.resolve_media("https://example.com/x", cfg, allow_browser=False)
    assert not media.ok


def test_list_series_without_tikhub(cfg):
    feed = wc.ChannelsFeed(ok=True, title="出狱后，我举世无双-第1集")
    items, note = wc.list_series(wc.parse_ref(SHARE_URL), cfg, feed)
    assert items == []
    assert "TikHub" in note


def test_list_series_localizes_missing_title_pattern(cfg):
    cfg.set("delivery.tikhub.api_key", "k")
    feed = wc.ChannelsFeed(ok=True, title="随手拍的一天")
    items, note = wc.list_series(wc.parse_ref(SHARE_URL), cfg, feed)
    assert items == []
    assert "剧集规律" in note


# ── 浏览器抓包解析 ────────────────────────────────────────────────────
def test_absorb_json_extracts_media_fields():
    capture = wc.BrowserCapture()
    wc._absorb_json(json.dumps({
        "data": {"feedInfo": {"h264VideoInfo": {
            "videoUrl": "https://finder.video.qq.com/251/20302/stodownload?x=1"},
            "description": "某某短剧-第2集"}},
        "decodeKey": "777",
    }), capture)
    assert capture.video_url.endswith("x=1")
    assert capture.decode_key == "777"
    assert capture.title == "某某短剧-第2集"


def test_absorb_json_ignores_garbage():
    capture = wc.BrowserCapture()
    wc._absorb_json("not json", capture)
    wc._absorb_json("", capture)
    assert capture.video_url == ""


# ── 从分享链接到「可下载源」的整条解析链 ──────────────────────────────
def test_share_link_survives_full_resolve_chain(monkeypatch, tmp_path):
    """回归测试：分享短链展开成落地页之后**不能再被当成短链**。

    真事故：``channels.weixin.qq.com`` 是 ``weixin.qq.com`` 的子域，短链按后缀
    匹配的话，落地页会被再展开一次 —— 而它已经是终点，展开不出东西，
    整单直接判定"短链没有跳转，可能已失效"。
    """
    from xydl import linkparse
    from xydl.config import Config
    from xydl.http import Response
    from xydl.resolver import Resolver

    share = "https://weixin.qq.com/sph/AJizVCRhAt"
    landing = "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AJizVCRhAt"

    parsed = linkparse.parse(share)
    assert parsed.links and parsed.links[0].kind == "short", parsed.links

    cfg = Config({"net": {"proxy": "none"},
                  "paths": {"data_dir": str(tmp_path / "d")}})
    resolver = Resolver(cfg)

    def fake_fetch(url, method):
        # 模拟 301 → 落地页；落地页本身不再跳转
        if url.rstrip("/") == share:
            return Response(status=301, url=landing, headers={},
                            chain=[share, landing])
        return Response(status=200, url=landing,
                        headers={"content-type": "text/html; charset=utf-8"},
                        body="<html><title>视频号</title></html>".encode(),
                        chain=[landing])

    monkeypatch.setattr(resolver, "_fetch", fake_fetch)

    outcome = resolver.resolve(parsed.links, parsed.title_guess)
    assert outcome.ok, outcome.note
    assert outcome.chosen is not None
    assert outcome.chosen.platform == "wechat_channels"
    assert outcome.chosen.kind == "page", "落地页必须判成普通页面，不能又是短链"
    assert wc.is_channels_url(outcome.chosen.url)


# ── object_id 剧集（S3）：不白问匿名接口，也不拿空 URL 去起浏览器 ────────
def test_resolve_media_skips_anonymous_api_for_object_id_only(monkeypatch, cfg):
    """只有 object_id 时匿名接口一定不支持，别白花一次请求（合集一集一次）。"""
    stub = _StubRequest([])
    monkeypatch.setattr(wc, "request", stub)

    ref = wc.ChannelsRef(object_id="14396973035218999573")
    media = wc.resolve_media("", cfg, ref=ref, allow_browser=False)

    assert not media.ok
    assert stub.calls == [], "只有 object_id 时不该去问匿名预览接口"
    assert "tikhub" in media.error.lower()


def test_resolve_media_object_id_only_uses_tikhub(monkeypatch, cfg):
    cfg.set("delivery.tikhub.api_key", "k")
    stub = _StubRequest([{"code": 200, "data": TIKHUB_DETAIL}])
    monkeypatch.setattr(wc, "request", stub)

    ref = wc.ChannelsRef(object_id="14396973035218999573")
    media = wc.resolve_media("", cfg, ref=ref, allow_browser=False)

    assert media.ok and media.via == "tikhub"
    assert stub.calls[0]["url"].endswith("fetch_video_detail")


def test_capture_via_browser_refuses_empty_url(cfg):
    """只有 object_id 的集没有可打开的页面地址。

    绝不能让空串流到 Edge：它会把空串当成"当前目录"，然后白等一整个超时，
    最后报一句"可能被安全软件拦截"——把排查方向带偏。
    """
    import time as _time

    ref = wc.ChannelsRef(object_id="123456789012345")
    started = _time.time()
    capture = wc.capture_via_browser(ref, cfg, wait_sec=5.0)
    elapsed = _time.time() - started

    assert not capture.video_url
    assert "object_id" in capture.error
    assert elapsed < 2.0, f"应该立刻返回，不该真的去起浏览器（耗时 {elapsed:.1f}s）"


# ── 翻页（M5）：终止条件只认游标 ──────────────────────────────────────
def test_paged_videos_stops_when_cursor_empty(monkeypatch):
    """游标为空就停。

    旧逻辑里 ``data.get("down_continue", bool(last_buffer))`` 在 down_continue
    是对象时 ``bool({...})`` 恒为真，服务端说"没有下一页"也会继续打满 6 页，
    白烧 API 额度。
    """
    calls: list[str] = []

    def fake_post(endpoint, payload, api_key, **kwargs):
        calls.append(str(payload.get("last_buffer")))
        return {"videos": [{"id": "1", "description": "某某短剧-第1集"}],
                "last_buffer": "",
                "down_continue": {"has_more": True}}, ""

    monkeypatch.setattr(wc, "_tikhub_post", fake_post)
    episodes, error = wc._paged_videos("/x", {"username": "u"}, "k", limit=50)
    assert error == ""
    assert len(episodes) == 1
    assert len(calls) == 1, f"游标为空必须立刻停，实际打了 {len(calls)} 页"


def test_paged_videos_follows_cursor(monkeypatch):
    """有游标就继续翻，并且把游标回传上去。"""
    pages = [
        ({"videos": [{"id": "1", "description": "第1集"}], "last_buffer": "CURSOR1"}, ""),
        ({"videos": [{"id": "2", "description": "第2集"}], "last_buffer": ""}, ""),
    ]
    seen: list[str] = []

    def fake_post(endpoint, payload, api_key, **kwargs):
        seen.append(str(payload.get("last_buffer")))
        return pages.pop(0)

    monkeypatch.setattr(wc, "_tikhub_post", fake_post)
    episodes, _ = wc._paged_videos("/x", {"username": "u"}, "k", limit=50)
    assert seen == ["", "CURSOR1"]
    assert [e.object_id for e in episodes] == ["1", "2"]


# ── 元数据缓存：一次订单别问两遍 ──────────────────────────────────────
def test_feed_info_is_cached(monkeypatch, cfg):
    """一次订单里「展开剧集」和「解析直链」都要 feed —— 只该问一次。"""
    stub = _StubRequest([PREVIEW_BODY, PREVIEW_BODY])
    monkeypatch.setattr(wc, "request", stub)

    first = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)
    second = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)

    assert first.ok and second.ok
    assert len(stub.calls) == 1, "第二次应命中缓存"
    assert second.title == first.title


def test_feed_failures_are_not_cached(monkeypatch, cfg):
    """失败不缓存：用户配好 TikHub/登录之后要能立刻拿到成功结果。"""
    stub = _StubRequest([
        {"errCode": -1, "errMsg": "permission verification failed"},
        PREVIEW_BODY,
    ])
    monkeypatch.setattr(wc, "request", stub)

    first = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)
    second = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg)

    assert not first.ok
    assert second.ok, "失败被缓存的话，用户配好 key 也拿不到新结果"
    assert len(stub.calls) == 2


# ── CDP 端口选择（M1）────────────────────────────────────────────────
def test_free_cdp_port_skips_busy_port():
    """端口被占时必须换一个，否则新起的 Edge 根本起不来、白等一个超时。"""
    import socket

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    busy = server.getsockname()[1]
    try:
        assert wc._free_cdp_port(busy) != busy
    finally:
        server.close()


# ── 封面图 vs 视频（严重的正确性坑）──────────────────────────────────
@pytest.mark.parametrize("url", [
    # 真实封面地址（实测预览页只会请求这个）
    "https://finder.video.qq.com/251/20350/stodownload?encfilekey=x&picformat=200",
    "https://finder.video.qq.com/251/20350/stodownload?encfilekey=x&wxampicformat=503",
    "https://wxapp.tc.qq.com/251/20304/stodownload?encfilekey=x&format=jpg",
    "https://finder.video.qq.com/251/20350/stodownload?encfilekey=x",
])
def test_cover_urls_are_not_treated_as_video(url):
    """封面和视频走同一个 stodownload 接口。

    早期实现"看到 stodownload 就当视频"，会把封面 JPEG 当视频交付 —— 而且是
    "看起来成功"的那种。这里把封面的特征（picformat / 20350 / 20304）钉死。
    """
    assert wc.looks_like_video_url(url) is False


@pytest.mark.parametrize("url", [
    # 真实视频地址（路径 20302，没有 picformat）
    "https://finder.video.qq.com/251/20302/stodownload?encfilekey=x&token=y",
    "https://finder.video.qq.com/251/20302/stodownload?encfilekey=x&bizid=1023",
    "https://finderlive.video.qq.com/aaa/stodownload?encfilekey=x",
])
def test_video_urls_are_accepted(url):
    assert wc.looks_like_video_url(url) is True


def test_empty_url_is_not_video():
    assert wc.looks_like_video_url("") is False


def test_absorb_json_ignores_cover_urls():
    """抓响应体时也不能把 coverUrl 当成 videoUrl。"""
    capture = wc.BrowserCapture()
    wc._absorb_json(json.dumps({
        "feedInfo": {"coverUrl": "https://finder.video.qq.com/251/20350/stodownload?x=1",
                     "description": "某某短剧-第1集"}}), capture)
    assert capture.video_url == "", "封面地址不能当视频"


# ── 关键：分享链接里的 token（能不能拿到 mp4 就靠它）──────────────────
#: 这是微信里"复制链接"得到的真实形态
FEED_URL_WITH_TOKEN = (
    "https://channels.weixin.qq.com/finder-preview/pages/feed"
    "?entry_card_type=48&comment_scene=39&appid=51"
    "&token=E%2FB8lggBEAMaIAgEEhwxNzkxNjA5MDkwMzkyNjY0MzM3NDkyeCtNd3FHIhgIAxIUCAMSEEi0"
    "a6ZIU2WDghsSOSp%2Fngw%3D&entry_scene=51"
    "&eid=export%2FUzFfBgAAxMOjaFVFf3ztjczT4DCgIqOuNAYs45q5tbHEMwxtPPNs04MPLg")
REAL_TOKEN = ("E/B8lggBEAMaIAgEEhwxNzkxNjA5MDkwMzkyNjY0MzM3NDkyeCtNd3FHIhgIAxIUCAMSEEi0"
              "a6ZIU2WDghsSOSp/ngw=")


def test_parse_ref_extracts_token():
    """``token`` 必须从链接里取出来 —— 这就是前面一直拿不到 mp4 的原因。

    同一个接口，``generalToken`` 传空字符串时服务端**剥离媒体字段**（只剩标题封面），
    传上 token 就下发 ``feedInfo.videoUrl``。URL 里的 token 是转义过的，要解码。
    """
    ref = wc.parse_ref(FEED_URL_WITH_TOKEN)
    assert ref.token == REAL_TOKEN
    assert ref.export_id.startswith("export/UzFfBgAAxMOjaFVF")
    assert ref.ok


def test_feed_request_carries_token(monkeypatch, cfg):
    """抓请求体：token 必须真的进了 ``baseReq.generalToken``，exportId 也要带上。"""
    captured: dict = {}

    def fake_post(body, config, **kwargs):
        captured.update(body)
        return {"errCode": 0, "data": {"feedInfo": {"description": "某剧-第1集"},
                                       "authorInfo": {}, "sceneInfo": {},
                                       "errMsg": {"type": 0}}}

    monkeypatch.setattr(wc, "_post_preview", fake_post)
    ref = wc.parse_ref(FEED_URL_WITH_TOKEN)
    wc._fetch_feed_info_uncached(ref, cfg)

    assert captured["baseReq"]["generalToken"] == REAL_TOKEN, \
        "token 没进 generalToken —— 服务端就会剥离媒体字段"
    assert captured["exportId"].startswith("export/")


def test_media_from_feed_dict_prefers_h264():
    """同一个 feed 里 h264 和 h265 都有时，选 h264：客户设备/剪映的兼容性高得多。"""
    info = {
        "videoUrl": "https://finder.video.qq.com/251/20302/stodownload?x=0",
        "h264VideoInfo": {"videoUrl": "https://finder.video.qq.com/251/20302/stodownload?x=h264"},
        "h265VideoInfo": {"videoUrl": "https://finder.video.qq.com/251/20302/stodownload?x=h265"},
    }
    url, _key = wc._media_from_feed_dict(info)
    assert url.endswith("x=h264")


def test_resolve_media_uses_token_link(tmp_path, cfg, monkeypatch):
    """带 token 的链接走「匿名接口」这一步就要拿到直链（不需要 TikHub/抓包/登录）。"""
    feed_body = {"errCode": 0, "data": {
        "feedInfo": {"description": "出狱后，我举世无双-第1集",
                     "coverUrl": "https://finder.video.qq.com/251/20350/stodownload?x=1",
                     "h264VideoInfo": {"videoUrl": CDN_VIDEO_URL},
                     "h265VideoInfo": {"videoUrl": CDN_VIDEO_URL + "&h265=1"}},
        "authorInfo": {"nickname": "邻里剧场"},
        "sceneInfo": {}, "errMsg": {"type": 0}}}

    monkeypatch.setattr(wc, "_post_preview",
                        lambda body, config, **kw: feed_body)
    media = wc.resolve_media(FEED_URL_WITH_TOKEN, cfg, allow_browser=False)
    assert media.ok, media.error
    assert media.url == CDN_VIDEO_URL
    assert media.via == "feed"
    assert "出狱后" in media.title
    assert media.decode_key == "", "这条路拿到的是明文 MP4，不需要 decode_key"


# ── 清晰度可选（而不是"默认取最高"）────────────────────────────────────
_SPEC_INFO = {
    "h264VideoInfo": {"videoUrl": "u264"},
    "h265VideoInfo": {"videoUrl": "u265"},
    "videoUrl": "uplain",
    "spec": [{"url": "u1080", "height": 1080, "fileFormat": "1080P"},
             {"url": "u720", "height": 720, "fileFormat": "720P"}],
}


def test_feed_media_candidates_lists_every_option():
    """必须把**所有**候选列出来，上层才能选；不能写死取第一个。"""
    labels = [c["label"] for c in wc.feed_media_candidates(_SPEC_INFO)]
    assert "h264" in labels and "h265" in labels
    assert "1080P" in labels and "720P" in labels, "spec[] 里的真实分辨率档位也要列出来"
    assert "uplain" in [c["url"] for c in wc.feed_media_candidates(_SPEC_INFO)]


@pytest.mark.parametrize("quality,expected", [
    ("1080", "u1080"),
    ("720", "u720"),          # 要 720 就给最接近且不低于的档，而不是给 1080
    ("480", "u720"),          # 要更低的档但没有，退到最低可用
])
def test_pick_media_by_resolution(quality, expected):
    assert wc.pick_media_candidate(_SPEC_INFO, quality)["url"] == expected


@pytest.mark.parametrize("quality,label", [
    ("best", "h264"), ("compatible", "h264"), ("small", "h265"), ("worst", "h265"),
])
def test_pick_media_by_codec_preference(quality, label):
    """没有分辨率的响应里，"可选"的维度是编码/体积 —— 按偏好给，而不是永远取第一个。"""
    info = {"h264VideoInfo": {"videoUrl": "u264"}, "h265VideoInfo": {"videoUrl": "u265"}}
    assert wc.pick_media_candidate(info, quality)["label"] == label


def test_pick_media_asked_1080_but_source_has_none():
    """要 1080 但源里没有：退化成默认编码，并把实际档位暴露出来（quality 字段）。"""
    info = {"h264VideoInfo": {"videoUrl": "u264"}, "h265VideoInfo": {"videoUrl": "u265"}}
    picked = wc.pick_media_candidate(info, "1080")
    assert picked["label"] == "h264"
    assert picked["height"] == "", "没有分辨率信息时要如实为空，不能编一个"


def test_media_from_feed_dict_keeps_old_behaviour():
    """旧调用点仍然要能用（默认兼容性优先）。"""
    assert wc._media_from_feed_dict({"videoUrl": "plain"})[0] == "plain"
    assert wc._media_from_feed_dict(
        {"media": [{"url": "m1", "decode_key": "k"}]}) == ("m1", "k")


# ── 元宝入口（短链 → 带 token 的 playable_url）────────────────────────
def test_yuanbao_js_fetch_escapes_newline():
    """回归：JS 单引号字符串里的换行必须转义成 ``\\n``。

    踩过的坑：Python 里写 ``'\\n'`` 会变成**真换行**放进 JS 字符串 → 整段脚本
    语法错误 → ``Runtime.evaluate`` 抛异常、拿不到 value → 报"元宝返回：(空)"。
    看上去像"接口没数据"，实际是拼字符串拼坏了，白查很久。
    """
    from xydl import yuanbao

    js = yuanbao._js_fetch("https://weixin.qq.com/sph/abc")
    assert "\\n" in js, "换行要转义"
    assert "+ '\n' +" not in js, "真换行会破坏 JS 字符串"
    assert "video_channel_url" in js, "请求体必须带 type 字段"
    assert "scene" in js, "请求体必须带 scene"


def test_yuanbao_request_body_shape():
    """请求体字段是从抓包里挖出来的：缺 type/scene 服务端只回全空骨架。"""
    from xydl import yuanbao

    js = yuanbao._js_fetch("https://weixin.qq.com/sph/abc")
    assert "{type: 'video_channel_url'" in js.replace('"', "'")


# ── 本地抓包通道（run.py channels-capture 截到的东西）──────────────────
CDN_VIDEO_URL = "https://finder.video.qq.com/251/20302/stodownload?encfilekey=abc"


def test_resolve_media_uses_local_capture(tmp_path, cfg, monkeypatch):
    """本地抓包截到结果时，不用 TikHub 也能拿到直链 + decode_key。

    这是"不依赖第三方"的唯一通道：微信 PC 客户端播一次 → 代理截下来 → 流水线用之。
    """
    import time

    from xydl import wxproxy

    store_path = tmp_path / "cap.json"
    cfg.set("download.wechat_channels.capture_store", str(store_path))
    store = wxproxy.CaptureStore(path=store_path)
    store.add({"url": CDN_VIDEO_URL, "decode_key": "2136343393",
               "title": "出狱后，我举世无双-第1集", "ts": time.time()})

    monkeypatch.setattr(wc, "request", _StubRequest([PREVIEW_BODY]))
    media = wc.resolve_media(SHARE_URL, cfg, allow_browser=False)

    assert media.ok, media.error
    assert media.via == "capture"
    assert media.url == CDN_VIDEO_URL
    assert media.decode_key == "2136343393"


def test_resolve_media_ignores_stale_capture(tmp_path, cfg, monkeypatch):
    """过期的抓包结果不能用：decode_key 每次请求都变，配错就是花屏文件。"""
    from xydl import wxproxy

    store_path = tmp_path / "cap.json"
    cfg.set("download.wechat_channels.capture_store", str(store_path))
    cfg.set("download.wechat_channels.capture_max_age_sec", 60)
    wxproxy.CaptureStore(path=store_path).add(
        {"url": CDN_VIDEO_URL, "decode_key": "1", "title": "旧的", "ts": 1.0})

    monkeypatch.setattr(wc, "request", _StubRequest([PREVIEW_BODY]))
    media = wc.resolve_media(SHARE_URL, cfg, allow_browser=False)
    assert not media.ok
    assert any("本地抓包" in a for a in media.attempts)


def test_capture_store_path_follows_config(tmp_path, cfg):
    from xydl import wxproxy

    cfg.set("download.wechat_channels.capture_store", str(tmp_path / "x.json"))
    assert wxproxy.capture_store_path(cfg) == tmp_path / "x.json"
    # 留空则落在数据目录（抓包服务与流水线必须指向同一个文件）
    cfg.set("download.wechat_channels.capture_store", "")
    assert wxproxy.capture_store_path(cfg).name == "wx_capture.json"


def test_extract_media_and_store_accept_real_feed_shape():
    """用真实的 finderassistant 响应形状串一遍：url + decodeKey 都要出来。"""
    from xydl import wxproxy

    payload = {"errCode": 0, "data": {"objectDesc": {"description": "第1集", "media": [
        {"url": CDN_VIDEO_URL, "decodeKey": "2136343393",
         "coverUrl": "https://finder.video.qq.com/251/20350/stodownload?picformat=200"}]}}}
    hits = wxproxy.extract_media(payload)
    assert len(hits) == 1
    assert hits[0].url == CDN_VIDEO_URL and hits[0].decode_key == "2136343393"


# ── 活体测试（默认跳过）────────────────────────────────────────────────
@pytest.mark.skipif(not os.environ.get("XYDL_LIVE_TESTS"),
                    reason="需要联网；设 XYDL_LIVE_TESTS=1 打开")
def test_live_preview_api(cfg):
    """对真实分享链接跑一次匿名预览接口（验证接口契约没漂移）。"""
    feed = wc.fetch_feed_info(wc.parse_ref(SHARE_URL), cfg, timeout=20)
    assert feed.ok, feed.error
    assert feed.title, "应该能拿到标题"
    assert feed.export_id.startswith("export/")
