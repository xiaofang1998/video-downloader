"""TikHub.io 第三方解析：抖音 / TikTok / 快手 无水印直链。

抖音、TikTok、快手的风控（a_bogus / ArgusSecurityPlugin 请求签名）本地逆向
不现实，yt-dlp 的解析器也已过时。TikHub.io 在服务端维护了这套签名算法，
通过它的 API 拿到无水印视频直链，再走本项目自带的直链下载即可。

需要 TikHub.io 的 API key（免费注册 + 每日签到领额度），填到
config.json 的 ``delivery.tikhub.api_key``。
"""

from __future__ import annotations

from .http import request

API_BASE = "https://api.tikhub.io"

#: 平台 → (端点, 请求参数名)
_ENDPOINTS = {
    "douyin": ("/api/v1/douyin/app/v3/fetch_one_video_by_share_url", "share_url"),
    "tiktok": ("/api/v1/tiktok/app/v3/fetch_one_video_by_share_url", "share_url"),
    "kuaishou": ("/api/v1/kuaishou/app/fetch_one_video_by_url", "share_text"),
}


def _platform_of(url: str) -> str:
    low = url.lower()
    if "douyin" in low or "iesdouyin" in low:
        return "douyin"
    if "tiktok" in low:
        return "tiktok"
    if "kuaishou" in low or "gifshow" in low or "chenzhongtech" in low:
        return "kuaishou"
    return ""


def resolve(url: str, api_key: str, timeout: float = 30.0) -> tuple[str, str]:
    """返回 ``(无水印视频直链, 标题)``；失败返回 ``("", 错误说明)``。"""
    if not api_key:
        return "", "未配置 TikHub API key"
    platform = _platform_of(url)
    if not platform:
        return "", "非抖音/TikTok/快手链接"

    endpoint, param = _ENDPOINTS[platform]
    try:
        resp = request(
            "GET", API_BASE + endpoint,
            params={param: url},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return "", f"TikHub 请求失败：{exc}"

    if data.get("code") != 200:
        return "", f"TikHub 返回错误：{data.get('message') or data.get('code')}"

    body = data.get("data") or {}

    # 快手：data.photos[0].main_mv_urls[0].url
    if platform == "kuaishou":
        photos = body.get("photos") or []
        if photos:
            urls = photos[0].get("main_mv_urls") or []
            if urls and urls[0].get("url"):
                return urls[0]["url"], str(photos[0].get("caption") or "")
        return "", "TikHub 返回里没有可用的视频地址"

    # 抖音 / TikTok：data.aweme_detail.video.play_addr_*.url_list[0]
    aweme = body.get("aweme_detail") or {}
    title = str(aweme.get("desc") or "")
    video = aweme.get("video") or {}
    for key in ("play_addr_265", "play_addr", "play_addr_h264", "download_addr"):
        urls = (video.get(key) or {}).get("url_list") or []
        if urls and urls[0]:
            return urls[0], title
    return "", "TikHub 返回里没有可用的视频地址"
