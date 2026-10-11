"""微信视频号（WeChat Channels）解析：分享链接 → 视频直链。

为什么单独一个模块
------------------
视频号跟别的平台有两点根本不同，塞进通用流程里会很难看：

1. **分享链接不是内容页**。``https://weixin.qq.com/sph/XXXX`` 会 301 到
   ``channels.weixin.qq.com/finder-preview/pages/sph?id=XXXX``，那是一个 React
   空壳页（正文里只有一个 ``<div id="app">``），既没有 ``<video>`` 也没有内嵌
   JSON。**yt-dlp 也没有视频号解析器**，所以不能指望它。

2. **匿名只能拿到元数据**。预览接口
   ``POST /finder-preview/api/feed/get_feed_info`` 不需要登录，能拿到标题、
   作者、封面、点赞数、``dynamicExportId``；但它**把媒体地址字段剥掉了**
   （页面播放器读的是 ``feedInfo.h264VideoInfo.videoUrl``，匿名响应里没有）。
   想要 mp4 必须有登录态或第三方解析。这条链路在本模块里做成**可插拔的多级
   降级**，每一级失败都留下明确原因，而不是静默失败。

3. **下载到的文件前 128 KiB 是加密的**，见 :mod:`xydl.wxdecrypt`。

三级媒体解析（``resolve_media``）
---------------------------------
* ``feed``    —— **主力**：在微信里"复制链接"得到的是
  ``/finder-preview/pages/feed?token=…&eid=…``。把链接里的 ``token`` 放进请求体的
  ``baseReq.generalToken``，同一个接口就会下发 ``feedInfo.videoUrl``（明文 MP4 直链，
  h264/h265 两种），**不需要登录、不需要抓包、也不需要解密**。
  反之 token 传空（只有 ``/sph/xxx`` 短链时）服务端会**剥离媒体字段**，只剩标题/封面 ——
  这是绕了很久才定位到的一步，全项目的"视频号能不能下"都取决于它。
* ``capture`` —— 兜底：本地抓包（见 :mod:`xydl.wxproxy`），
  需要让微信 PC 客户端播一次视频；抓到的 ``spec[]`` 还能给出真实分辨率档位。
* ``browser`` —— 用 CDP 打开登录态 Edge 抓媒体请求。
  **2026-10 实测：对视频号无效，已默认关闭**（``download.wechat_channels.use_browser``）。
  证据（都在真实链接上跑出来的）：
    · 预览页 ``finder-preview/pages/sph?id=…`` 在**登录状态下也只渲染封面图 + 二维码**，
      页面上写着"可扫码前往微信观看此内容"，整页 28 个请求里没有任何视频请求
      （唯一命中的 ``stodownload`` 是封面图，路径 ``/20350/``）；
    · ``/web/pages/feed?eid=…`` 匿名请求返回 ``{"errCode":10012 非法请求}``，
      拿着有效的视频号助手登录态打开也拿不到 feed / 媒体请求；
    · 登录入口 ``/platform`` 是「视频号助手（创作者后台）」，与消费者侧网页播放
      不是同一套授权。
  代码保留（换账号/场景或腾讯改版后可能重新有用），但默认关掉 —— 否则每个视频号
  订单都会白等 25 秒 + 白弹一个浏览器窗口。

多剧集（``list_series``）
-------------------------
视频号的"合集"和"同一部短剧的第 N 集"是两回事，但代下载场景里两种都要支持：

* 视频号**没有公开的匿名作品列表接口**，所以"自动列出整部剧"离不开客户端上下文
  （注入/抓包）。本项目**不依赖任何第三方付费解析服务**，因此只做手里材料能支撑的事：
  标题层面的剧集识别（``XXX-第1集`` → 文件名/话术提示）+ 明确告诉运营"把各集的
  元宝解析链接一起粘进来即可批量下"。
* 详见 :func:`list_series` 的说明。
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import linkparse
from .config import Config
from .http import DEFAULT_UA, request
from .utils import clean_media_title, snippet

#: 匿名预览接口（从 finder-preview 的 JS bundle 里反解出来的真实调用）
PREVIEW_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
#: 预览页基址
PREVIEW_PAGE = "https://channels.weixin.qq.com/finder-preview/pages/sph"
#: 网页版 feed 页（**需要登录态**，未登录会返回 errCode 10012 非法请求）
WEB_FEED_PAGE = "https://channels.weixin.qq.com/web/pages/feed"
#: 扫码登录入口 —— **视频号助手**（`/platform`）。
#:
#: 踩过的坑：一开始我把登录入口写成 ``/web/pages/home``，实测那个地址匿名访问
#: 返回的是 ``{"desc":"非法请求","errCode":10012}`` 这段 **JSON**（content-type
#: 是 application/json），浏览器打开就是一片空白，**根本没有二维码**，扫码登录
#: 自然无从谈起。`/platform` 才返回 4729 字节的 HTML 外壳（真正的登录页）。
WEB_HOME_PAGE = "https://channels.weixin.qq.com/platform"

#: 预览接口要求的 Origin / Referer。**两个都缺一不可**：
#: 缺任一 → ``{"errCode":-1,"errMsg":"permission verification failed"}``（实测）。
API_ORIGIN = "https://channels.weixin.qq.com"
API_REFERER = "https://channels.weixin.qq.com/"

#: 视频号 CDN 特征
CDN_HOSTS = ("finder.video.qq.com", "wxapp.tc.qq.com", "finderlive.video.qq.com")

#: 分享短码：/sph/<code>
_SPH_PATH_RE = re.compile(r"/sph/([A-Za-z0-9_\-]+)", re.I)
#: 裸短码（运营直接粘一串码时）
_BARE_CODE_RE = re.compile(r"^[A-Za-z0-9_\-]{6,32}$")
#: export id
_EXPORT_RE = re.compile(r"export/[A-Za-z0-9_\-+/=]+")

#: "第N集 / 第N话 / EP12 / P3 / (1)" 这类剧集编号
#: 名字部分允许为空（"第3话"这种没名字的标题也要能识别）
_EPISODE_RE = re.compile(
    r"^(?P<name>.*?)"
    r"(?:[\s\-—_·:：]*"
    r"(?:第\s*(?P<num1>\d{1,4})\s*[集话期篇部]"
    r"|(?:EP|Ep|ep|E|P|p|#)\s*(?P<num2>\d{1,4})"
    r"|[(（【\[]\s*(?P<num3>\d{1,4})\s*[)）】\]]"
    r"|\s+(?P<num4>\d{1,4})))\s*$"
)

#: 浏览器抓包时要盯的媒体请求特征
_MEDIA_MARKS = ("stodownload", "finder.video.qq.com", "get_media", "video.qq.com")

#: **图片**地址的特征。视频号 CDN 上封面和视频走同一个 ``stodownload`` 接口，
#: 差别只在参数上：封面是 ``picformat=200`` / ``wxampicformat=`` / 路径 ``/2035x/``，
#: 视频是路径 ``/20302/``。
#:
#: 为什么必须区分（真踩过的坑）：预览页在**登录状态下也只渲染封面图**，而封面的
#: 请求同样命中 ``stodownload``。早期实现"看到 stodownload 就当视频"，会把
#: 一张封面 JPEG 当成视频下载下来交付 —— 而且是"看起来成功"的那种。
_IMAGE_MARKS = (
    "picformat=", "wxampicformat=", "format=jpg", "format=png", "format=webp",
    "/20350/", "/20304/", "/20306/", "/20308/",
)


def looks_like_video_url(url: str) -> bool:
    """这个 CDN 地址是**视频**还是封面图。

    只按参数/路径判断，不需要响应体 —— 抓包时第一条命中的往往就是封面。
    """
    low = (url or "").lower()
    if not low:
        return False
    if any(mark in low for mark in _IMAGE_MARKS):
        return False
    return ("stodownload" in low or "get_media" in low
            or "/20302/" in low or "finderlive" in low)

LogFn = Callable[[str], None]


def _noop_log(_message: str) -> None:
    return None


# ── 匿名元数据缓存 ────────────────────────────────────────────────────
#: 一次订单里「展开剧集」和「解析直链」都会去问一次匿名接口；不缓存就是白花请求。
_FEED_CACHE: dict[tuple[str, str], tuple[float, "ChannelsFeed"]] = {}
_FEED_CACHE_LOCK = threading.Lock()
_FEED_CACHE_TTL = 300.0
_FEED_CACHE_MAX = 256


def clear_feed_cache() -> None:
    """清掉匿名元数据缓存（测试与"换 key 后立刻重试"用）。"""
    with _FEED_CACHE_LOCK:
        _FEED_CACHE.clear()


# ══════════════════════════════════════════════════════════════════════
#  链接识别
# ══════════════════════════════════════════════════════════════════════
def is_channels_url(url: str) -> bool:
    """是不是视频号链接（分享短链 / 预览页 / 网页版 feed 页）。"""
    if not url:
        return False
    host = (linkparse.host_of(url) or "").lower()
    if not host:
        return False
    # 先判 channels.weixin.qq.com：它也是 .weixin.qq.com 的子域，顺序反了会漏判
    if host == "channels.weixin.qq.com" or host.endswith(".channels.weixin.qq.com"):
        # 整个域都是视频号（含 web/pages/home 这类登录页）——
        # 认出来比"未知站点"好：至少能给出「需要登录」这种可执行的提示。
        return True
    if host == "weixin.qq.com" or host.endswith(".weixin.qq.com"):
        return bool(_SPH_PATH_RE.search(url))
    return False


def is_channels_cdn_url(url: str) -> bool:
    """是不是视频号 CDN 的**媒体直链**（``finder.video.qq.com/.../stodownload``）。

    这一类和"分享链接"要分开处理：本地抓包工具（wx_channels_download、mitmproxy 等）
    给出来的就是这种直链。它下下来的文件**前 128 KiB 是加密的**，必须配
    ``decode_key`` 解密，否则交付出去就是花屏文件。
    """
    host = (linkparse.host_of(url) or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in CDN_HOSTS)


@dataclass
class ChannelsRef:
    """一条视频号链接拆出来的定位信息。"""

    short_uri: str = ""          # 分享短码，如 AJizVCRhAt
    export_id: str = ""          # export/UzFfBgAA...（会过期）
    object_id: str = ""          # 纯数字作品 id（剧集列表里只有它时会用到）
    #: 分享链接里的 ``token``。**这是能不能拿到 mp4 的关键**：
    #: 在微信里"复制链接"得到的是 ``/finder-preview/pages/feed?token=…&eid=…``，
    #: 把它放进请求体的 ``baseReq.generalToken`` 后，同一个接口就会下发
    #: ``feedInfo.videoUrl``（明文 MP4 直链）。传空字符串时服务端会**剥离媒体字段**，
    #: 只剩标题和封面 —— 这就是绕了一大圈才找到的那一半。
    token: str = ""
    original_url: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.short_uri or self.export_id or self.object_id)

    @property
    def channel_id(self) -> str:
        """视频号的 ``channel_id``：``sph`` + 短码。"""
        return f"sph{self.short_uri}" if self.short_uri else ""

    @property
    def share_url(self) -> str:
        """规范的分享短链（各个消费侧入口都认这种形态）。"""
        return f"https://weixin.qq.com/sph/{self.short_uri}" if self.short_uri else ""

    @property
    def preview_url(self) -> str:
        """预览页地址（匿名可访问）。"""
        if self.short_uri:
            return f"{PREVIEW_PAGE}?id={self.short_uri}"
        return self.original_url

    @property
    def web_feed_url(self) -> str:
        """网页版 feed 地址（需要登录态）。"""
        if self.export_id:
            return f"{WEB_FEED_PAGE}?eid={urllib.parse.quote(self.export_id, safe='')}"
        return self.preview_url


def parse_ref(url: str) -> ChannelsRef:
    """从 URL（或裸短码）里拆出定位信息。**不联网**。"""
    ref = ChannelsRef(original_url=url or "")
    text = (url or "").strip()
    if not text:
        ref.error = "空链接"
        return ref

    # 裸短码
    if _BARE_CODE_RE.match(text):
        ref.short_uri = text
        return ref

    # finder-preview / web feed 页：取 query
    if "?" in text:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query)
        for key in ("id", "shortUri", "short_uri"):
            if query.get(key):
                value = query[key][0].strip()
                if value and not value.startswith("export/"):
                    ref.short_uri = value
                    break
        if not ref.short_uri:
            for key in ("eid", "exportId", "export_id"):
                if query.get(key):
                    ref.export_id = query[key][0].strip()
                    break
        # token 必须取！在微信里"复制链接"得到的就是带 token 的 /feed 页，
        # 有了它同一个接口才会下发 videoUrl（明文 MP4）。早期这里写了
        # "token 参数不需要"，结果一直只能拿到标题和封面。
        if query.get("token"):
            ref.token = query["token"][0].strip()

    match = _SPH_PATH_RE.search(text)
    if match and not ref.short_uri:
        ref.short_uri = match.group(1)

    if not ref.export_id:
        found = _EXPORT_RE.search(text)
        if found:
            ref.export_id = found.group(0)

    # 纯数字目录段也可能是 object_id（channels.weixin.qq.com/.../<20位数字>）
    if not ref.ok:
        digits = re.search(r"/(\d{15,})", text)
        if digits:
            ref.object_id = digits.group(1)

    if not ref.ok:
        ref.error = f"认不出视频号短码：{snippet(text, 80)}"
    return ref


# ══════════════════════════════════════════════════════════════════════
#  元数据（匿名可用）
# ══════════════════════════════════════════════════════════════════════
@dataclass
class ChannelsFeed:
    """预览接口拿到的作品信息。"""

    ok: bool = False
    title: str = ""
    author: str = ""
    cover_url: str = ""
    export_id: str = ""
    username: str = ""
    object_id: str = ""
    createtime: int = 0
    like_count: str = ""
    playable: bool = False
    video_url: str = ""
    decode_key: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def describe(self) -> str:
        if not self.ok:
            return self.error or "未获取到视频号信息"
        bits = [self.title or "（无标题）"]
        if self.author:
            bits.append(f"@{self.author}")
        if self.playable:
            bits.append("含直链")
        return " · ".join(bits)


def _post_preview(body: dict[str, Any], config: Config, *,
                  timeout: float | None = None) -> dict[str, Any]:
    """调匿名预览接口。返回原始 JSON（失败时 ``{"__error__": ...}``）。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        # Origin + Referer 缺一不可，见模块 docstring
        "Origin": API_ORIGIN,
        "Referer": API_REFERER,
    }
    try:
        resp = request(
            "POST", PREVIEW_API, json_body=body, headers=headers,
            timeout=timeout or float(config.get("net.timeout_sec", 15) or 15),
            verify_tls=bool(config.get("net.verify_tls", True)),
            user_agent=str(config.get("net.user_agent", "") or DEFAULT_UA),
            max_bytes=512 * 1024,
        )
    except Exception as exc:  # noqa: BLE001 - 网络异常不能炸穿流水线
        return {"__error__": f"{type(exc).__name__}: {exc}"}

    if resp.error:
        return {"__error__": resp.error}
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 - 不是 JSON 就当失败
        return {"__error__": f"接口返回的不是 JSON（HTTP {resp.status}）"}


def _first_url(*values: Any) -> str:
    """从若干候选里挑第一个非空字符串。"""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def fetch_feed_info(ref: ChannelsRef | str, config: Config, *,
                    timeout: float | None = None) -> ChannelsFeed:
    """匿名拿标题/作者/封面/exportId。

    **这是唯一不需要登录的视频号接口**，所以即使媒体解析失败也要拿到它 ——
    至少能让运营知道"这单是什么内容"，文件名也有得取。

    结果带 5 分钟缓存：一次订单里「展开剧集」和「解析直链」两处都要用它，
    不缓存就是白花一次请求（合集一集一次更明显）。
    """
    if isinstance(ref, str):
        ref = parse_ref(ref)

    cache_key = (ref.short_uri, ref.export_id)
    with _FEED_CACHE_LOCK:
        cached = _FEED_CACHE.get(cache_key)
        if cached and time.time() - cached[0] < _FEED_CACHE_TTL:
            return cached[1]

    feed = _fetch_feed_info_uncached(ref, config, timeout=timeout)
    # 只缓存成功结果：失败往往是"还没登录/临时抖动"，缓存下来会让用户
    # 配好 key 之后仍然拿到旧的失败结论。
    if feed.ok:
        with _FEED_CACHE_LOCK:
            _FEED_CACHE[cache_key] = (time.time(), feed)
            _prune_feed_cache()
    return feed


def _prune_feed_cache() -> None:
    """清掉过期项；条目仍然过多时再按时间淘汰最旧的（服务长期运行不涨内存）。"""
    now = time.time()
    for key in [k for k, (ts, _) in _FEED_CACHE.items() if now - ts >= _FEED_CACHE_TTL]:
        _FEED_CACHE.pop(key, None)
    if len(_FEED_CACHE) > _FEED_CACHE_MAX:
        for key, _ in sorted(_FEED_CACHE.items(), key=lambda kv: kv[1][0])[
                : len(_FEED_CACHE) - _FEED_CACHE_MAX]:
            _FEED_CACHE.pop(key, None)


def _fetch_feed_info_uncached(ref: ChannelsRef, config: Config, *,
                              timeout: float | None = None) -> ChannelsFeed:
    feed = ChannelsFeed(export_id=ref.export_id)
    if not ref.ok:
        feed.error = ref.error or "链接里没有可用的视频号标识"
        return feed
    feed.export_id = ref.export_id

    body: dict[str, Any] = {"baseReq": {"generalToken": ref.token or ""}}
    if ref.short_uri:
        body["shortUri"] = ref.short_uri
    elif ref.export_id:
        body["exportId"] = ref.export_id
    else:
        body["objectId"] = ref.object_id

    data = _post_preview(body, config, timeout=timeout)
    if "__error__" in data:
        feed.error = str(data["__error__"])
        return feed

    if data.get("error"):
        err = data["error"] or {}
        feed.error = str(err.get("message") or err.get("name") or "接口返回错误")
        return feed
    if int(data.get("errCode") or 0) != 0:
        feed.error = str(data.get("errMsg") or f"errCode={data.get('errCode')}")
        return feed

    payload = data.get("data") or {}
    info = payload.get("feedInfo") or {}
    author = payload.get("authorInfo") or {}
    scene = payload.get("sceneInfo") or {}
    err_msg = payload.get("errMsg") or {}

    feed.raw = payload
    feed.title = clean_media_title(str(info.get("description") or ""), 80)
    feed.author = str(author.get("nickname") or "")
    feed.cover_url = str(info.get("coverUrl") or "")
    feed.createtime = int(info.get("createtime") or 0)
    feed.like_count = str(info.get("likeCountFmt") or info.get("likeCount") or "")
    if scene.get("dynamicExportId"):
        feed.export_id = str(scene["dynamicExportId"])
    feed.username = str(payload.get("username") or "")
    feed.object_id = str(payload.get("id") or "")

    # errMsg.type: 0 = 正常可预览；非 0 = 这条内容在预览场景不可用
    # （短码写错、内容被删、或需要微信客户端）。这种情况**不能算成功** ——
    # 上层据此决定是"有标题可用"还是"直接报内容不可用"。
    type_code = int(err_msg.get("type") or 0)
    if type_code != 0:
        feed.ok = False
        feed.error = f"这条视频号内容在预览场景不可用（errMsg.type={type_code}）"
    else:
        feed.ok = True

    # 顺手把媒体字段捞出来 —— 不同场景下匿名响应偶尔**会**带
    video_url, decode_key = _media_from_feed_dict(info)
    feed.video_url = video_url
    feed.decode_key = decode_key
    feed.playable = bool(video_url)
    return feed


#: 视频号清晰度偏好 → 候选编码的优先级。
#: 实测纯 HTTP 这条路上，服务端**只给一档分辨率**（源里那一档），
#: 但会给两种编码（h264 / h265），所以"可选"的维度目前是编码/体积：
#:   best        —— 体积码率最高的（h264，画质略好、兼容性最好）
#:   compatible  —— 同 best，但明确表达"要哪儿都能播"（默认）
#:   small       —— h265，同分辨率下体积小一半左右
#: 如果响应里带了 ``spec[]``（客户端抓包那条路才有），会按分辨率优先选，
#: 这时 1080/720 这种**真实分辨率档位**才可选。
_QUALITY_PREFERENCE: dict[str, tuple[str, ...]] = {
    "best": ("h264", "h265", "plain"),
    "high": ("h264", "h265", "plain"),
    "compatible": ("h264", "h265", "plain"),
    "1080": ("h264", "h265", "plain"),
    "720": ("h264", "h265", "plain"),
    "480": ("h265", "h264", "plain"),          # 小档优先省流量的编码
    "small": ("h265", "h264", "plain"),
    "worst": ("h265", "h264", "plain"),
}


def feed_media_candidates(info: dict[str, Any]) -> list[dict[str, str]]:
    """列出这条 feed 里**所有**可下载的媒体候选（而不是直接挑一个）。

    这样上层才能"按偏好选"而不是"写死取第一个"。每一项形如::

        {"label": "h264", "url": "...", "decode_key": "", "height": "1280"}

    ``height`` 只有 ``spec[]`` 那种带分辨率字段的响应才有值（客户端抓包路线）。
    """
    if not isinstance(info, dict):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(label: str, url: str, key: str = "", height: str = "") -> None:
        url = str(url or "")
        if not url or url in seen:
            return
        seen.add(url)
        out.append({"label": label, "url": url, "decode_key": str(key or ""),
                    "height": str(height or "")})

    for label, node_key in (("h264", "h264VideoInfo"), ("h265", "h265VideoInfo")):
        node = info.get(node_key) or {}
        if isinstance(node, dict):
            add(label, node.get("videoUrl") or "", node.get("decodeKey") or "")

    # spec[]：客户端接口才有，带真实分辨率/清晰度名
    for spec in info.get("spec") or []:
        if not isinstance(spec, dict):
            continue
        url = _first_url(spec.get("url"), spec.get("full_url"),
                         spec.get("videoUrl"))
        if not url:
            continue
        height = str(spec.get("height") or spec.get("videoHeight") or "")
        name = str(spec.get("fileFormat") or spec.get("spec") or spec.get("label")
                   or spec.get("definition") or "")
        add(name or f"spec{height}", url, spec.get("decode_key") or "", height)

    add("plain", info.get("videoUrl") or "", info.get("decodeKey") or "")
    for item in info.get("media") or []:
        if isinstance(item, dict):
            add("media", _first_url(item.get("url"), item.get("full_url")),
                item.get("decode_key") or "", str(item.get("height") or ""))
    return out


def pick_media_candidate(info: dict[str, Any], quality: str = "") -> dict[str, str]:
    """按偏好从候选里挑一个；挑不到就退化成第一个候选。"""
    candidates = feed_media_candidates(info)
    if not candidates:
        return {}
    want = str(quality or "").strip().lower()
    order = _QUALITY_PREFERENCE.get(want, _QUALITY_PREFERENCE["compatible"])

    # 明确要某个分辨率时先按分辨率挑（只有 spec[] 带 height 才可能命中）
    if want.isdigit():
        target = int(want)
        exact = [c for c in candidates
                 if c.get("height").isdigit() and int(c["height"]) >= target]
        if exact:
            return min(exact, key=lambda c: int(c["height"]))
        hd = [c for c in candidates if c.get("height").isdigit()]
        if hd:
            return max(hd, key=lambda c: int(c["height"]))

    for label in order:
        for cand in candidates:
            if cand["label"] == label:
                return cand
    return candidates[0]


def _media_from_feed_dict(info: dict[str, Any]) -> tuple[str, str]:
    """从 feedInfo 里抠出 ``(videoUrl, decode_key)``（兼容旧调用，默认 h264 优先）。"""
    cand = pick_media_candidate(info)
    if cand:
        return cand["url"], cand["decode_key"]
    return "", ""


# ══════════════════════════════════════════════════════════════════════
#  浏览器（登录态 CDP）解析
# ══════════════════════════════════════════════════════════════════════
class BrowserCapture:
    """一次浏览器抓包的产物。"""

    def __init__(self) -> None:
        self.video_url = ""
        self.decode_key = ""
        self.title = ""
        self.cookies: dict[str, str] = {}
        self.error = ""
        self.visited: list[str] = []


def capture_via_browser(ref: ChannelsRef, config: Config, *,
                        profile: Path | None = None,
                        wait_sec: float = 25.0,
                        log: LogFn = _noop_log) -> BrowserCapture:
    """用 CDP 打开登录态 Edge，抓 ``finder.video.qq.com`` 的媒体请求。

    ⚠️ **2026-10 实测对视频号无效**（详见模块 docstring）。默认关闭，
    只有把 ``download.wechat_channels.use_browser`` 打开才会走到这里。
    保留它的另一个理由：这套"开浏览器 + 抓网络"的机制本身是好的，
    换站点/换场景可以复用。

    失败时返回的 ``capture.error`` 说明需要先登录，而不是含糊的超时。
    实现上复用 :mod:`xydl.cdp_fetch` 里那套纯标准库 WebSocket CDP 客户端，
    避免再引入一个浏览器自动化依赖。
    """
    from .cdp_fetch import _CDP, _find_edge, _http_json, login_profile_dir, _pick_port

    capture = BrowserCapture()
    edge = _find_edge()
    if not edge:
        capture.error = "没找到 Edge 浏览器，无法用登录态抓取"
        return capture

    target_profile = Path(profile) if profile else login_profile_dir("wechat_channels")
    target_profile.mkdir(parents=True, exist_ok=True)

    # 打开网页版 feed 页：只有登录后的这个页面才会真正播放并请求视频流。
    # 预览页（finder-preview）刻意不放视频（只显示"扫码前往微信观看"），
    # 所以没有 exportId 时也去 feed 页试一下 —— 拿不到 exportId 的链接
    # 至少能靠匿名接口拿到标题。
    target_url = ref.web_feed_url
    if not target_url:
        # 绝不把空串当 URL 传给 Edge：Edge 会把它当成"当前目录"，然后我们
        # 等 25 秒超时、报一句"可能被安全软件拦截"——把排查方向带偏。
        capture.error = ("这一集没有可打开的页面地址（只有 object_id），"
                         "无法用浏览器抓取；请在元宝里解析出带 token/eid 的链接再粘回本工具")
        return capture
    port = _free_cdp_port(_pick_port() + 11)
    log(f"视频号：用登录态浏览器打开 {target_url}")

    proc = None
    cdp = None
    try:
        proc = subprocess.Popen(
            [edge, f"--remote-debugging-port={port}",
             f"--user-data-dir={target_profile}", "--no-first-run",
             "--no-default-browser-check", target_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        page = None
        deadline = time.time() + min(wait_sec, 30.0)
        while time.time() < deadline:
            time.sleep(1)
            try:
                pages = _http_json(f"http://127.0.0.1:{port}/json")
            except Exception:  # noqa: BLE001 - 浏览器还没起来
                continue
            page = next((p for p in pages if p.get("type") == "page"
                         and "weixin" in str(p.get("url", ""))), None)
            if page:
                break
        if not page:
            capture.error = "浏览器没有打开视频号页面（可能被安全软件拦截）"
            return capture

        cdp = _CDP(page["webSocketDebuggerUrl"], timeout=10.0)
        cdp.call("Network.enable")
        cdp.call("Page.enable")
        # **必须重新导航一次**：我们是"事后"才 attach 到已经打开的页面的，
        # 页面很可能在我们开始监听之前就加载完了 —— 那样一个请求都看不到
        # （实测：等待 12 秒，visited=0，永远抓不到直链）。
        # 重新加载一次，才能观察到页面 JS 发出的 feed 接口请求与媒体请求。
        cdp.call("Page.navigate", {"url": target_url})

        deadline = time.time() + wait_sec
        seen: set[str] = set()
        pending: dict[str, str] = {}

        def interesting(url: str) -> bool:
            low = url.lower()
            if any(mark in low for mark in _MEDIA_MARKS):
                return True
            if "/cgi-bin/" in low or "get_feed_info" in low:
                return True
            # 网页版是**同域接口**下发 ``feedInfo.h264VideoInfo.videoUrl`` 的
            # （页面 bundle 里读的就是这个字段），媒体地址不一定以独立请求出现
            # （播放器可能走 MSE）。所以整个视频号域名的响应体都值得读一遍 ——
            # 视频号页面请求量很小，漏读才是大损失。
            return "channels.weixin.qq.com" in low

        while time.time() < deadline:
            event = cdp.wait_event(
                lambda d: d.get("method", "").startswith("Network."), timeout=3)
            if not event:
                continue
            params = event.get("params") or {}
            request_id = params.get("requestId")
            if event["method"] == "Network.requestWillBeSent":
                url = str((params.get("request") or {}).get("url") or "")
                if url and interesting(url) and url not in seen:
                    seen.add(url)
                    capture.visited.append(url)
                    pending[request_id] = url
                    # 只认视频地址：封面的 stodownload 也在这里出现，绝不能当视频
                    if looks_like_video_url(url) and not capture.video_url:
                        capture.video_url = url
            elif event["method"] == "Network.responseReceived":
                # 有响应头时再用 content-type 兜一道：image/* 一律不算视频
                url = str((params.get("response") or {}).get("url") or "")
                ctype = str(((params.get("response") or {}).get("headers") or {})
                            .get("content-type") or ((params.get("response") or {})
                                                     .get("mimeType") or "")).lower()
                if url and capture.video_url == url and ctype.startswith("image/"):
                    capture.video_url = ""
            elif event["method"] == "Network.loadingFinished" and request_id in pending:
                url = pending.pop(request_id)
                try:
                    body = cdp.call("Network.getResponseBody", {"requestId": request_id})
                    text = str((body.get("result") or {}).get("body") or "")
                except Exception:  # noqa: BLE001
                    continue
                _absorb_json(text, capture)
            if capture.video_url and capture.decode_key:
                break

        # 把登录态 cookie 一起带走：CDN 有时要求 Referer/Cookie
        try:
            cookies = cdp.call("Network.getAllCookies")
            for item in ((cookies.get("result") or {}).get("cookies") or []):
                domain = str(item.get("domain") or "")
                if "weixin.qq.com" in domain or "qq.com" in domain:
                    capture.cookies[str(item.get("name"))] = str(item.get("value"))
        except Exception:  # noqa: BLE001 - cookie 拿不到不影响主流程
            pass

        if not capture.video_url:
            capture.error = (
                "浏览器里没有抓到视频直链。这条通道对视频号**实测无效**：预览页只渲染"
                "封面图 + 二维码（'可扫码前往微信观看此内容'），网页版 feed 页匿名返回 "
                "10012、带登录态也拿不到媒体请求。请在元宝里解析出带 token/eid 的链接。"
            )
            log(f"视频号：浏览器里探测到 {len(capture.visited)} 个相关请求，"
                f"但没有媒体地址")
        else:
            log(f"视频号：抓到直链，decode_key "
                f"{'已拿到' if capture.decode_key else '没拿到（可能需要额外解密）'}")
    except Exception as exc:  # noqa: BLE001 - 浏览器路径不能炸穿主流程
        capture.error = f"浏览器抓取出错：{type(exc).__name__}: {exc}"
    finally:
        if cdp is not None:
            try:
                cdp.close()
            except Exception:  # noqa: BLE001
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
    return capture


def _free_cdp_port(start: int, tries: int = 60) -> int:
    """从 ``start`` 开始找一个当前没人监听的本地端口给 CDP 用。

    为什么不能直接用 ``9400 + pid % 300``：视频号抓包、抖音 CDP 兜底、扫码登录
    三处都是同一个公式，在同一个进程里先后调用会算出**完全一样**的端口。
    而 Edge 的调试端口一旦已被占用，新起的进程根本起不来，我们又要白等一整个
    超时，还报一句误导性的"可能被安全软件拦截"。所以这里逐个试探。
    """
    import socket

    for offset in range(max(1, tries)):
        candidate = start + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.2)
                if sock.connect_ex(("127.0.0.1", candidate)) != 0:
                    return candidate
        except OSError:
            return candidate
    return start


def _absorb_json(text: str, capture: BrowserCapture) -> None:
    """从抓到的响应体里捞 ``decode_key`` / ``videoUrl`` / 标题。"""
    if not text or ("decode" not in text and "videoUrl" not in text
                    and "description" not in text):
        return
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 - 不是 JSON 就算了
        return

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("videoUrl", "full_url", "play_url"):
                value = node.get(key)
                if isinstance(value, str) and value.startswith("http") \
                        and "qq.com" in value and not capture.video_url:
                    capture.video_url = value
            if not capture.decode_key and node.get("decode_key"):
                capture.decode_key = str(node["decode_key"])
            if not capture.decode_key and node.get("decodeKey"):
                capture.decode_key = str(node["decodeKey"])
            if not capture.title and node.get("description"):
                capture.title = clean_media_title(str(node["description"]), 80)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)


# ══════════════════════════════════════════════════════════════════════
#  媒体解析门面
# ══════════════════════════════════════════════════════════════════════
@dataclass
class ChannelsMedia:
    """解析出来的可下载媒体。"""

    ok: bool = False
    url: str = ""
    title: str = ""
    decode_key: str = ""
    file_size: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    via: str = ""
    #: 实际选中的档位标签（h264 / h265 / spec 名…），用于在界面/话术里告诉用户
    #: "你要的 1080P 源里没有，实际拿到的是 720P"。用户提的是"分辨率可选而不是
    #: 默认最高"，所以选了什么必须可见。
    quality: str = ""
    feed: ChannelsFeed | None = None
    error: str = ""
    attempts: list[str] = field(default_factory=list)


def _channel_config(config: Config) -> dict[str, Any]:
    node = config.get("download.wechat_channels", {}) or {}
    return node if isinstance(node, dict) else {}


def resolve_media(url: str, config: Config, *,
                  feed: ChannelsFeed | None = None,
                  ref: ChannelsRef | None = None,
                  log: LogFn = _noop_log,
                  allow_browser: bool = True) -> ChannelsMedia:
    """把视频号链接解析成可下载的直链。多级降级，失败时给出可执行的原因。"""
    settings = _channel_config(config)
    if not bool(settings.get("enabled", True)):
        return ChannelsMedia(error="配置里关闭了 download.wechat_channels.enabled")

    ref = ref or parse_ref(url)
    media = ChannelsMedia(feed=feed)
    if not ref.ok:
        media.error = ref.error
        return media

    # ⓪ 链接里没有 token 时（用户粘的是 /sph/xxx 短链），先用元宝换一条
    #    "带 token 的 playable_url" —— 这是短链能下视频的唯一入口。
    #    元宝的鉴权在页面上下文里（cookie 里只有分析类），所以必须在页内 fetch，
    #    见 :mod:`xydl.yuanbao`。
    if not ref.token and bool(settings.get("yuanbao_enabled", True)):
        try:
            from . import yuanbao as _yuanbao

            playable, yb_error = _yuanbao.parse_playable_url(
                url, log=log,
                headless=bool(settings.get("yuanbao_headless", False)))
            if not playable:
                # 页内 fetch 拿不到时，改成"把链接发进元宝对话框"——
                # 元宝的解析本来就是收到消息才触发的（实测）。
                log("元宝：页内解析没成功，改用发送消息的方式重试")
                playable, yb_error = _yuanbao.parse_via_chat(url, log=log)
            if playable:
                got_ref = parse_ref(playable)
                if got_ref.token and got_ref.export_id:
                    ref = got_ref
                    feed = _fetch_feed_info_uncached(ref, config)
                    media.feed = feed
                    if feed.title:
                        media.title = feed.title
                    log("视频号：元宝解析出带 token 的链接，直接取直链")
                else:
                    media.attempts.append("元宝：返回的链接里没有 token/eid")
            else:
                media.attempts.append(f"元宝：{yb_error}")
        except Exception as exc:                 # 元宝是入口增强项，坏了不能拖垮解析
            media.attempts.append(f"元宝：{type(exc).__name__}: {exc}")

    # 匿名预览接口只认 shortUri / exportId；只有 objectId 时它一定失败
    # （实测），别白花一次请求 —— 合集一集一次，50 集就是 50 次。
    anonymous_usable = bool(ref.short_uri or ref.export_id)
    if feed is None:
        if anonymous_usable:
            feed = fetch_feed_info(ref, config)
        else:
            feed = ChannelsFeed(error="这一集只有 object_id，匿名预览接口不支持")
        media.feed = feed
    if feed.ok and feed.title:
        media.title = feed.title

    # ⓪(前置) 清晰度：把这条 feed 里**所有**候选列出来，按偏好选，而不是写死取一个。
    #      纯 HTTP 这条路服务端只给一档分辨率（源里那一档）+ 两种编码，
    #      所以现在的"可选"维度是编码/体积；带 spec[] 的响应（客户端抓包）才有
    #      真实分辨率档位可选。
    _quality_want = str(settings.get("quality")
                        or config.get("download.quality") or "compatible")
    if feed is not None and feed.raw:
        _info = feed.raw.get("feedInfo") or {}
        _cands = feed_media_candidates(_info)
        if len(_cands) > 1:
            log("视频号可选档位：" + "、".join(c["label"] for c in _cands))
        _picked = pick_media_candidate(_info, _quality_want)
        if _picked:
            feed.video_url = _picked["url"]
            feed.decode_key = _picked["decode_key"]
            media.quality = str(_picked["label"])
            log(f"视频号：清晰度偏好={_quality_want} → 选中 {_picked['label']}")

    # ① 匿名接口（带 token 时）直接给直链
    if feed.video_url:
        media.ok = True
        media.url = feed.video_url
        media.decode_key = feed.decode_key
        media.via = "feed"
        log("视频号：匿名接口直接下发了直链")
        return media

    # 只有 object_id 的剧集：拿不到 shortUri/exportId 就没有匿名入口，
    # 也没有浏览器能打开的页面地址 —— 直接说清楚该走哪条路，别让用户干等。
    if not anonymous_usable:
        media.attempts.append("匿名接口：不支持只有 object_id 的链接")
        media.error = ("；".join(media.attempts)
                       + "｜这一集只能按 object_id 定位：请在元宝里解析出带 "
                         "token/eid 的链接再粘回本工具")
        return media

    # ② 本地抓包（`run.py channels-capture` 截到的 url + decode_key）
    #    微信 PC 客户端播一次，我们把它截下来 —— 全程自研、不依赖付费服务。
    #    函数内导入 wxproxy：wxproxy 反过来 import 本模块，模块级导入会成环。
    try:
        from . import wxproxy as _wxproxy

        store = _wxproxy.CaptureStore(path=_wxproxy.capture_store_path(config))
        max_age = float(settings.get("capture_max_age_sec", 1800) or 1800)
        # ① 抓到的**全档位**优先：客户端接口的 spec[] 才有真实分辨率可选，
        #    纯 HTTP 那条路服务端只给一档（且签名和档位绑定，改标记无效）。
        spec = store.best_spec(str(settings.get("quality") or
                                   config.get("download.quality") or ""),
                               max_age=max_age)
        if spec:
            media.ok = True
            media.url = str(spec.get("url") or "")
            media.decode_key = str(spec.get("decode_key") or "")
            media.quality = str(spec.get("height") or spec.get("label") or "")
            media.title = media.title or str(spec.get("title") or "")
            media.via = "capture-spec"
            log(f"视频号：用抓到的清晰度档位 {media.quality}p（自己选的，不是默认最高）")
            return media
        entry = None
        if ref.export_id:
            entry = store.find(ref.export_id, max_age=max_age)
        if entry is None and media.title:
            entry = store.find(series_prefix(media.title), max_age=max_age)
        if entry is None:
            entry = store.latest(max_age=max_age)
        if entry:
            media.ok = True
            media.url = str(entry.get("url") or "")
            media.decode_key = str(entry.get("decode_key") or "")
            media.title = media.title or str(entry.get("title") or "")
            media.file_size = int(entry.get("file_size") or 0)
            media.via = "capture"
            log(f"视频号：用本地抓包结果（{'有密钥' if media.decode_key else '无密钥'}）")
            return media
        media.attempts.append("本地抓包：还没有截到这条视频（先在微信 PC 里播一次）")
    except Exception as exc:                     # 抓包是增强项，坏了不能拖垮解析
        media.attempts.append(f"本地抓包：{type(exc).__name__}: {exc}")

    # ④ 登录态浏览器抓包（实测对视频号无效，默认关闭）
    if allow_browser and bool(settings.get("use_browser", True)):
        wait_sec = float(settings.get("browser_wait_sec", 25) or 25)
        capture = capture_via_browser(ref, config, wait_sec=wait_sec, log=log)
        if capture.video_url:
            media.ok = True
            media.url = capture.video_url
            media.decode_key = capture.decode_key
            media.title = media.title or capture.title
            media.via = "browser"
            if capture.cookies:
                media.headers["Cookie"] = "; ".join(
                    f"{k}={v}" for k, v in capture.cookies.items())
            media.headers["Referer"] = f"{API_ORIGIN}/"
            log("视频号：登录态浏览器抓到直链")
            return media
        media.attempts.append(f"浏览器：{capture.error or '没有抓到媒体请求'}")

    media.error = "；".join(media.attempts) or "没有可用的视频号解析通道"
    if not ref.token:
        # 视频号短链不带 token，服务端就是不下发媒体。产品流程是**引导客户自己
        # 过一遍元宝**：把链接粘进元宝 → 元宝回一条带 token 的直链 → 粘回来就能下。
        media.error += (
            "｜这是**视频号短链**，它本身不带 token，服务端不会下发视频。"
            "请按下面三步走一次（免费，一次即可）："
            "① 打开腾讯元宝 https://yuanbao.tencent.com/ 并登录；"
            "② 把这条链接粘进元宝的对话框发出去；"
            "③ 元宝会回一条形如 "
            "`channels.weixin.qq.com/finder-preview/pages/feed?token=…&eid=…` 的链接，"
            "把它复制后**粘回本工具**，就能直接下载（明文 MP4）。"
        )
    else:
        media.error += ("｜链接里已经带了 token，但仍取不到媒体：可能已过期"
                        "（token 有时效），回元宝重新解析一次即可。")
    return media


# ══════════════════════════════════════════════════════════════════════
#  剧集识别 / 多剧集列表
# ══════════════════════════════════════════════════════════════════════
def series_episode_number(title: str) -> int | None:
    """从标题里识别"第几集"。

    >>> series_episode_number("出狱后，我举世无双-第1集")
    1
    >>> series_episode_number("某某短剧 EP12")
    12
    >>> series_episode_number("随手拍的一天") is None
    True
    """
    if not title:
        return None
    match = _EPISODE_RE.match(str(title).strip())
    if not match:
        return None
    for key in ("num1", "num2", "num3", "num4"):
        value = match.group(key)
        if value:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def series_prefix(title: str) -> str:
    """取"剧名"部分（去掉集数标记）。

    >>> series_prefix("出狱后，我举世无双-第1集")
    '出狱后，我举世无双'
    >>> series_prefix("某某短剧 EP12")
    '某某短剧'
    """
    if not title:
        return ""
    match = _EPISODE_RE.match(str(title).strip())
    name = (match.group("name") if match else str(title)).strip()
    return name.rstrip(" -—_·:：").strip()


@dataclass
class ChannelEpisode:
    """一个待下载的视频号作品。"""

    index: int
    title: str = ""
    object_id: str = ""
    export_id: str = ""
    url: str = ""

    @property
    def share_url(self) -> str:
        return self.url or f"https://weixin.qq.com/sph/{self.export_id}"


def list_series(ref: ChannelsRef, config: Config, feed: ChannelsFeed, *,
                limit: int = 50, log: LogFn = _noop_log
                ) -> tuple[list[ChannelEpisode], str]:
    """列出同一部剧的其它集。返回 ``(集列表, 说明)``。

    **视频号没有公开的匿名作品列表接口**，所以任何"自动列出整部剧"的方案都得靠
    客户端上下文（注入/抓包）。本工具不依赖第三方付费服务，因此这里只做
    **手里已有材料**能支撑的事：

    * 标题里有 ``第 N 集`` 这种规律 → 用于命名与话术提示（见 ``series_prefix``）
    * 真正的批量下载：把每一集在元宝里解析出的链接**一起粘进本工具**（一行一条），
      管线会当成多条链接逐条下好并按集号命名

    拿不到就返回空列表 + 一句人话，**绝不假装成功**（假装成功比报错更坑：
    买家以为在下一整部，结果只下了一集）。
    """
    if series_episode_number(feed.title) is None:
        return [], "这条标题里没有「第 N 集」规律，看起来不是剧集"
    prefix = series_prefix(feed.title)
    if not prefix:
        return [], "标题里只有集数没有剧名，无法按剧名归组"
    log(f"视频号：识别到剧名「{prefix}」，但视频号没有公开的作品列表接口 —— "
        f"要批量下请把各集的元宝链接一起粘进来（一行一条）")
    return [], (f"视频号没有公开的作品列表接口，无法自动列出整部剧；"
                f"把「{prefix}」各集的元宝解析链接一起粘进来即可批量下载")


