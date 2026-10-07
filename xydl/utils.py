"""杂项工具：文件名清洗、去重、文本处理。"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

#: Windows 文件名非法字符
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Windows 保留设备名（大小写不敏感，带扩展名也不行）
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
#: emoji 与不可见字符
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\u200b-\u200f\ufeff\ufe0f\u2b00-\u2bff]"
)


def clean_media_title(title: str, maxlen: int = 60) -> str:
    """把平台给的元数据标题收拾成人能用的。

    真实案例：抖音的 ``title`` 字段是「一行标题 + 换行 + 整段简介」，
    直接拿来命名会得到一个 ~66 字符、塞满《》@ 和整段文案的文件名。
    取第一行、限长，就干净了。

    >>> clean_media_title("吹笛子建议收藏\\n这是一首融合竹笛与爵士律动的跨界作品")
    '吹笛子建议收藏'
    >>> clean_media_title("")
    ''
    """
    if not title:
        return ""
    first = next((ln.strip() for ln in str(title).splitlines() if ln.strip()), "")
    first = re.sub(r"\s{2,}", " ", first)
    if len(first) > maxlen:
        first = first[:maxlen]
    # 尾部的标点一律削掉（不只是截断时）—— 文件名以「。」或「···」结尾很难看
    return first.rstrip(" .-—_·•|,，。:：")


#: 这些「标题」是占位符而不是真标题，不能拿来给文件命名
PLACEHOLDER_TITLES = frozenset(
    {"video", "untitled", "unknown", "download", "media", "clip", "video.mp4"}
)

#: 自动生成的占位标题：形如 "XiaoHongShu video #6ab896ff0000000018007f59"
_GENERIC_TITLE_RE = re.compile(
    r"(?i)"
    r"\b(?:video|media|clip|download|untitled)\s*[#＃]?\s*[0-9a-f]{8,}\b"
    r"|\b[0-9a-f]{20,}\b"          # 一长串 hex id（笔记/视频 id）
)


def is_generic_title(title: str) -> bool:
    """平台给的标题是不是自动生成的占位名。

    实测：小红书的 yt-dlp 元数据标题是 ``XiaoHongShu video #6ab896ff...``，
    而买家分享文案里的「当你开始注重 你将拥有紧致有型的身材」明显更好。
    这种情况就该用后者。

    >>> is_generic_title("XiaoHongShu video #6ab896ff0000000018007f59")
    True
    >>> is_generic_title("当你开始注重 你将拥有紧致有型的身材")
    False
    >>> is_generic_title("")
    True
    """
    if not title or not str(title).strip():
        return True
    text = str(title).strip()
    if text.lower() in PLACEHOLDER_TITLES:
        return True
    return bool(_GENERIC_TITLE_RE.search(text))


def pick_best_title(metadata_title: str, share_title: str,
                    maxlen: int = 60) -> str:
    """在「平台元数据标题」和「买家分享文案里解析的标题」之间挑更好的。

    平台元数据通常更准，但偶尔是自动生成的占位名，这时分享文案反而更好。
    """
    meta = clean_media_title(metadata_title, maxlen)
    share = clean_media_title(share_title, maxlen)
    if not is_generic_title(meta):
        return meta
    if not is_generic_title(share):
        return share
    return meta or share


def sanitize_filename(name: str, maxlen: int = 80, fallback: str = "video") -> str:
    """把任意字符串变成 Windows/Linux 都能安全落盘的文件名（不含扩展名）。

    >>> sanitize_filename('猫咪/打呼噜:合集?')
    '猫咪_打呼噜_合集'
    >>> sanitize_filename('   ')
    'video'
    """
    if not name:
        return fallback
    out = unicodedata.normalize("NFC", str(name))
    out = _EMOJI.sub("", out)
    out = _ILLEGAL.sub("_", out)
    # 折叠空白，去掉首尾的点和空格（Windows 不允许以点或空格结尾）
    out = re.sub(r"\s+", " ", out).strip(" .")
    out = re.sub(r"_{2,}", "_", out).strip(" _")
    if len(out) > maxlen:
        out = out[:maxlen].rstrip(" .")
    if not out:
        return fallback
    if out.split(".")[0].upper() in _RESERVED:
        out = f"_{out}"
    return out


def ensure_unique(path: Path) -> Path:
    """目标已存在时追加 -1 / -2 …"""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(1, 1000):
        candidate = parent / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem}-{int(datetime.now().timestamp())}{suffix}"


def display_path(path: str | Path, root: str | Path | None = None) -> str:
    """尽量给出相对路径，日志里更好读。"""
    p = Path(path)
    if root:
        try:
            return str(p.relative_to(Path(root)))
        except ValueError:
            return str(p)
    return str(p)


def snippet(text: str, n: int = 60) -> str:
    """截断长文本，用于日志与列表展示。"""
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[: n - 1] + "…"


def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.now().strftime(fmt)


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def excerpt_html_title(html: str) -> str:
    """从 HTML 里抠出标题：优先 og:title，退化到 <title>。"""
    if not html:
        return ""
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r"<title[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = re.sub(r"\s+", " ", match.group(1)).strip()
            if title:
                return title
    return ""


def excerpt_meta_url(html: str) -> str:
    """有些分享页不靠 302，而是在 HTML 里塞规范链接或 JS 跳转。"""
    if not html:
        return ""
    patterns = (
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']',
        r'(?i)window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
        r'(?i)location\.replace\(\s*["\']([^"\']+)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            url = match.group(1).strip()
            if url.startswith("http"):
                return url
    return ""
