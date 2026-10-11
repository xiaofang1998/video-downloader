"""X / Twitter 兜底解析：游客态拿不到 media 时，用第三方镜像接口拿直链。

**为什么需要这个模块**

X 对「受限推文」（需登录才能查看、或带年龄门槛的内容），给游客 token 返回的
GraphQL JSON 里**没有 media**，yt-dlp 于是报::

    ERROR: [twitter] 2109142587022229998: No video could be found in this tweet

但媒体本身是公开的：`api.fxtwitter.com` 不需要登录就能给出
`video.twimg.com/amplify_video/.../xxx.mp4` 的直链（实测 HTTP 200、
`video/mp4`、首字节 `ftyp`）。所以这里做一层兜底：
**yt-dlp 失败时**再走它，正常推文仍然由 yt-dlp 处理（元数据更全、能选格式）。

注意：镜像接口是第三方服务，随时可能变；所以这里的解析写得**很宽容**
（两种返回结构都认，字段缺失也不抛），失败就如实返回错误，由上层决定话术。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .http import request

#: `x.com/<user>/status/<id>`、`twitter.com/i/status/<id>`、`/statuses/<id>`
_STATUS_RE = re.compile(
    r"(?:^|/)(?:i|([A-Za-z0-9_]{1,20}))/status(?:es)?/(\d{6,25})", re.I)
_TWITTER_HOSTS = ("x.com", "twitter.com", "mobile.twitter.com", "www.x.com",
                  "www.twitter.com")

#: 镜像接口（按顺序试）。它们返回同一种信息的两种结构，解析都兼容。
_MIRRORS = ("https://api.fxtwitter.com/{user}/status/{tid}",
            "https://api.vxtwitter.com/{user}/status/{tid}")


def is_twitter(url: str) -> bool:
    """这条链接是不是 X / Twitter。"""
    text = (url or "").lower()
    return any(host in text for host in _TWITTER_HOSTS)


def parse_status(url: str) -> tuple[str, str] | None:
    """从链接里抠出 ``(user, tweet_id)``；抠不到返回 ``None``。

    ``user`` 可能是空串（`x.com/i/status/<id>` 这种形式没有用户名）。
    """
    match = _STATUS_RE.search(url or "")
    if not match:
        return None
    return (match.group(1) or ""), match.group(2)


def _pick_size(node: Any) -> tuple[int, int]:
    """从各种可能的字段里取 (宽, 高)，取不到给 0。"""
    if not isinstance(node, dict):
        return 0, 0
    size = node.get("size") if isinstance(node.get("size"), dict) else node
    def num(*names: str) -> int:
        for name in names:
            value = size.get(name)
            if isinstance(value, (int, float)) and value:
                return int(value)
        return 0
    return num("width", "w"), num("height", "h")


def extract_media(payload: Any) -> list[dict[str, Any]]:
    """把镜像接口的返回**宽容地**映射成统一的媒体列表。

    兼容两种结构::

        fxtwitter: {"tweet": {"text": …, "media": {"videos": [...], "photos": [...]}}}
        vxtwitter: {"text": …, "media_extended": [{"type": "video", "url": …}]}

    每项形如 ``{"url", "kind", "width", "height", "duration", "thumbnail"}``；
    视频排在前、按分辨率从高到低（上层默认取第一条就是最好的）。
    """
    tweet = payload.get("tweet") if isinstance(payload, dict) else None
    root = tweet if isinstance(tweet, dict) else (payload if isinstance(payload, dict) else {})
    media = root.get("media") if isinstance(root.get("media"), dict) else {}

    items: list[dict[str, Any]] = []

    def add(url: Any, kind: str, node: Any = None, thumb: Any = None) -> None:
        if not isinstance(url, str) or not url.startswith("http"):
            return
        width, height = _pick_size(node if isinstance(node, dict) else {})
        duration = 0
        if isinstance(node, dict):
            value = node.get("duration") or node.get("duration_millis")
            if isinstance(value, (int, float)):
                duration = int(value / 1000) if value > 10000 else int(value)
        items.append({"url": url, "kind": kind, "width": width, "height": height,
                      "duration": duration, "thumbnail": thumb or ""})

    for node in (media.get("videos") or []):
        if isinstance(node, dict):
            add(node.get("url"), "video", node, node.get("thumbnail_url"))
    for node in (root.get("media_extended") or []):
        if isinstance(node, dict) and str(node.get("type", "")).lower() == "video":
            add(node.get("url"), "video", node, node.get("thumbnail_url"))
    # vxtwitter 的扁平形态：mediaURLs 里全是直链
    for url in (root.get("mediaURLs") or []):
        if not any(item["url"] == url for item in items):
            add(url, "video")

    for node in (media.get("photos") or []):
        if isinstance(node, dict):
            add(node.get("url"), "photo", node)
    for node in (root.get("media_extended") or []):
        if isinstance(node, dict) and str(node.get("type", "")).lower() in ("image", "photo"):
            add(node.get("url"), "photo", node)

    videos = [item for item in items if item["kind"] == "video"]
    photos = [item for item in items if item["kind"] != "video"]
    # 按**总像素**排（不能只看 height：竖屏 720x1280 的 height 比横屏 1414x720 大，
    # 按 height 排会把竖屏低清排到前面，实测踩过）。
    videos.sort(key=lambda item: (item["width"] or 0) * (item["height"] or 0),
                reverse=True)
    return videos + photos


def fetch_media(url: str, *, timeout: float = 20.0, verify_tls: bool = True,
                fetch: Callable[[str], str] | None = None
                ) -> tuple[list[dict[str, Any]], str, str]:
    """查询镜像接口取媒体。

    返回 ``(媒体列表, 推文文案, 错误说明)``。成功时错误为空；失败时列表为空。
    ``fetch`` 可注入（测试用），默认走 :func:`xydl.http.request`。
    """
    parsed = parse_status(url)
    if not parsed:
        return [], "", "链接里找不到推文 ID（要形如 x.com/<用户名>/status/<数字>）"
    user, tid = parsed
    if not user:
        user = "i"      # x.com/i/status/<id> 也能查
    problems: list[str] = []
    for template in _MIRRORS:
        target = template.format(user=user, tid=tid)
        try:
            if fetch is not None:
                raw = fetch(target)
            else:
                raw = request("GET", target, timeout=timeout, verify_tls=verify_tls).text
            payload = json.loads(raw)
        except Exception as exc:               # noqa: BLE001 - 镜像挂了就试下一个
            problems.append(f"{template.split('/')[2]}：{type(exc).__name__}")
            continue
        items = extract_media(payload)
        if items:
            tweet = payload.get("tweet") if isinstance(payload, dict) else None
            root = tweet if isinstance(tweet, dict) else payload
            text = str((root or {}).get("text") or "").strip()
            return items, text, ""
        code = payload.get("code") if isinstance(payload, dict) else None
        message = payload.get("message") if isinstance(payload, dict) else None
        problems.append(f"{template.split('/')[2]}：没有媒体"
                        + (f"（code={code} {message}）" if code or message else ""))
    return [], "", "；".join(problems) or "镜像接口都失败了"
