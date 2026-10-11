"""去水印支持：优先取「无水印源」，拿不到时如实标注；必要时用 ffmpeg 像素级擦除。

**两种"去水印"在工程上完全不同，不能混为一谈**

1. **取无水印源（首选，无损）**
   平台接口常常同时给出带水印 / 无水印两份地址。抖音的 ``playwm``（play
   watermark）与 ``play`` 就是**同一份视频**的两个地址；URL 参数里带
   ``watermark=1`` 的也是同一回事。把标记换掉 / 去掉参数，就得到干净的那份。
   这条路**不损失任何画质**，也是本工具抖音/TikTok/快手通道一直在用的思路
   （本项目的自研通道拿到的就是无水印直链）。

2. **像素级擦除（兜底，有损）**
   水印已经"烧"进画面（视频号、部分下载源、用户自带素材）时，只能用
   ffmpeg ``delogo`` 拿周边像素**插值盖住**那块区域：会留下淡淡的痕迹，
   且**局部画质有下降**。所以它**不默认开**，只在用户明确要求时对成片使用。

本模块只做两件纯函数级别的事（便于测试、也便于别处复用）：
``pick_source`` 选源，``rect_for`` 算 delogo 矩形。
"""
from __future__ import annotations

from typing import Iterable

#: URL 层面能识别出"带水印"的特征 → 对应的无水印写法。
#: 这些是抖音/TikTok 系常见的地址形态（``playwm`` = play with watermark）。
_WM_REWRITES: tuple[tuple[str, str], ...] = (
    ("/playwm/", "/play/"),
    ("playwm", "play"),
    ("with_watermark=1", "with_watermark=0"),
    ("is_watermark=1", "is_watermark=0"),
    ("watermark=1", "watermark=0"),
)

#: 命中这些参数名就认为"这条地址可能带水印"。
_WM_PARAM_KEYS = ("watermark", "with_watermark", "is_watermark", "wm")

#: 平台 → delogo 预设（**比例**，按视频分辨率换算；水印基本都在右下角）。
#: ``w``/``h`` 是水印区域占画面的比例，``mx``/``my`` 是距右/下边缘的留白比例。
#: 只是"出厂估计"，真实水印大小随平台改版变化，所以命令行允许 ``--rect`` 精确定位。
DELOGO_PRESETS: dict[str, dict[str, float | str]] = {
    "douyin": {"label": "抖音（右下角）", "w": 0.26, "h": 0.050, "mx": 0.02, "my": 0.02},
    "tiktok": {"label": "TikTok（右下角）", "w": 0.26, "h": 0.050, "mx": 0.02, "my": 0.02},
    "kuaishou": {"label": "快手（右下角）", "w": 0.26, "h": 0.050, "mx": 0.02, "my": 0.02},
    "wechat_channels": {"label": "微信视频号（右下角）",
                        "w": 0.30, "h": 0.055, "mx": 0.02, "my": 0.02},
    "xiaohongshu": {"label": "小红书（右下角）",
                    "w": 0.24, "h": 0.060, "mx": 0.02, "my": 0.03},
}


def looks_watermarked(url: str) -> bool:
    """这条直链看起来是不是"带水印"的那份。

    判据保守：只认已知的地址特征（``playwm`` 路径、``watermark=1`` 之类），
    认不出来就当作"不清楚"，不硬猜 —— 猜错会把干净源排到后面。
    """
    text = (url or "").lower()
    if not text:
        return False
    if "/playwm/" in text or "playwm" in text:
        return True
    for key in _WM_PARAM_KEYS:
        if f"{key}=1" in text or f"{key}=true" in text:
            return True
    return False


def clean_variants(url: str) -> list[str]:
    """把带水印地址改写成无水印候选（不含原地址；原地址由调用方保底）。

    只做**等价替换**：``playwm``→``play``、``watermark=1``→``0``。
    改写出来的地址可能 404（平台不保证两份都在），所以调用方要按顺序试。
    """
    if not url:
        return []
    out: list[str] = []
    for old, new in _WM_REWRITES:
        if old in url:
            candidate = url.replace(old, new)
            if candidate != url and candidate not in out:
                out.append(candidate)
    return out


def order_candidates(urls: Iterable[str], *, prefer_no_watermark: bool = True
                     ) -> list[str]:
    """把候选直链排成"无水印优先"，并去掉重复与空值。

    除了原地址，还会把每条带水印地址的**改写版本**插进来（``clean_variants``），
    因为改写版往往才是真正干净的那份。
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def push(value: str) -> None:
        value = (value or "").strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)

    for url in urls:
        if not url:
            continue
        if prefer_no_watermark:
            for candidate in clean_variants(url):
                push(candidate)
            if not looks_watermarked(url):
                push(url)
        else:
            push(url)
    # 带水印的原地址永远兜底放在最后（万一干净版都拿不到，至少能下下来）
    for url in urls:
        if url and looks_watermarked(url):
            push(url)
    return ordered


def pick_source(urls: Iterable[str], *, prefer_no_watermark: bool = True
                ) -> tuple[str, str]:
    """从候选里挑一条下载地址，并给出**给用户看的说明**。

    返回 ``(url, note)``；``note`` 会写进订单交付信息，让用户知道拿到的是哪种：

    * ``""`` —— 正常拿到无水印源（或平台本来就没水印），不用特别说明
    * ``"带水印源"`` —— 只有带水印的那份，如实告知，别让用户以为被做了处理
    """
    candidates = order_candidates(urls, prefer_no_watermark=prefer_no_watermark)
    if not candidates:
        return "", ""
    chosen = candidates[0]
    if prefer_no_watermark and looks_watermarked(chosen):
        return chosen, "带水印源"
    return chosen, ""


def rect_for(platform: str, width: int, height: int,
             *, rect: str = "") -> tuple[int, int, int, int]:
    """算 delogo 的 ``(x, y, w, h)``。

    ``rect`` 给定时优先用它（``x:y:w:h`` 直接照抄），否则按平台预设的比例换算。
    没给平台也没有 rect → 抛 ``ValueError``（让调用方给出明确提示，别静默处理错区域）。
    """
    if rect:
        parts = [part.strip() for part in rect.replace(",", ":").split(":")]
        if len(parts) != 4:
            raise ValueError("--rect 要写成 x:y:w:h（例如 760:1790:300:100）")
        try:
            x, y, w, h = (int(float(part)) for part in parts)
        except ValueError as exc:
            raise ValueError("--rect 的四个值都要是数字") from exc
        if w <= 0 or h <= 0:
            raise ValueError("--rect 的宽高必须大于 0")
        return x, y, w, h

    preset = DELOGO_PRESETS.get((platform or "").strip().lower())
    if not preset:
        known = "、".join(sorted(DELOGO_PRESETS))
        raise ValueError(f"不认识平台「{platform}」；可用：{known}；或用 --rect 手动指定")
    if width <= 0 or height <= 0:
        raise ValueError("拿不到视频分辨率，无法按预设换算 —— 用 --rect 手动指定")

    w = max(1, int(round(width * float(preset["w"]))))
    h = max(1, int(round(height * float(preset["h"]))))
    x = max(0, width - w - int(round(width * float(preset["mx"]))))
    y = max(0, height - h - int(round(height * float(preset["my"]))))
    # 双数坐标更友好（部分编码器要求偶数）
    return x - x % 2, y - y % 2, w - w % 2 or 2, h - h % 2 or 2


def delogo_filter(x: int, y: int, w: int, h: int) -> str:
    """生成 delogo 滤镜串。"""
    return f"delogo=x={x}:y={y}:w={w}:h={h}"


def preset_label(platform: str) -> str:
    """预设的人话名字（给日志/帮助用）。"""
    preset = DELOGO_PRESETS.get((platform or "").strip().lower())
    return str(preset["label"]) if preset else ""


#: 水印处理模式（下载时的选项；WebUI 与命令行共用这套值）
WATERMARK_MODES: dict[str, str] = {
    "auto": "优先无水印源；只有带水印版本时如实标注（默认）",
    "off": "只要无水印源；拿不到就报错（不交付带水印的）",
    "delogo": "优先无水印源；只有带水印版本时用 ffmpeg 擦掉（有损）",
    "any": "不挑，拿到哪个算哪个（不标注）",
}


def guess_platform(url: str) -> str:
    """从链接猜平台（用于选 delogo 预设）。猜不出返回空串。"""
    text = (url or "").lower()
    if "douyin.com" in text or "iesdouyin" in text:
        return "douyin"
    if "tiktok.com" in text:
        return "tiktok"
    if "kuaishou.com" in text or "gifshow.com" in text:
        return "kuaishou"
    if "channels.weixin.qq.com" in text or "weixin.qq.com/sph" in text:
        return "wechat_channels"
    if "xiaohongshu.com" in text or "xhslink" in text:
        return "xiaohongshu"
    return ""


def probe_size(ffmpeg: str, path) -> tuple[int, int]:
    """读视频分辨率。优先用同目录的 ffprobe，没有就解析 ``ffmpeg -i`` 输出。

    ``(0, 0)`` 表示拿不到 —— 调用方应提示"用 --rect 手动指定"，别硬猜。
    """
    import re
    import subprocess
    from pathlib import Path

    target = Path(path)
    probe = Path(ffmpeg).with_name("ffprobe.exe"
                                   if str(ffmpeg).lower().endswith(".exe") else "ffprobe")
    if probe.exists():
        try:
            out = subprocess.run(
                [str(probe), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0",
                 str(target)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ).stdout
            match = re.search(r"(\d+)\D+(\d+)", out or "")
            if match:
                return int(match.group(1)), int(match.group(2))
        except (OSError, subprocess.SubprocessError):
            pass
    try:                       # 退路：ffmpeg -i 会把流信息打到 stderr
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(target)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", proc.stderr or "")
        if match:
            return int(match.group(1)), int(match.group(2))
    except (OSError, subprocess.SubprocessError):
        pass
    return 0, 0


def delogo_file(ffmpeg: str, src, *, platform: str = "", rect: str = "",
                out=None, dry_run: bool = False) -> tuple[bool, str, str]:
    """用 ffmpeg ``delogo`` 把水印区域盖掉。返回 ``(成功, 产物路径, 说明)``。

    这是**有损**处理：用周边像素插值填充，会留淡淡痕迹、局部画质下降。所以只在
    用户明确要求（``--watermark delogo`` / ``run.py delogo``）时才做。

    ``platform`` 走预设比例、``rect`` 直接给 ``x:y:w:h``；两者都没有就报错 ——
    绝不"猜一个区域"乱盖（盖错地方比不盖更糟）。
    """
    import subprocess
    from pathlib import Path

    source = Path(src)
    if not source.is_file():
        return False, "", f"找不到文件：{source}"
    width, height = probe_size(ffmpeg, source)
    try:
        x, y, w, h = rect_for(platform, width, height, rect=rect)
    except ValueError as exc:
        return False, "", str(exc)

    target = Path(out) if out else source.with_name(f"{source.stem}.nowm{source.suffix}")
    if dry_run:
        return True, str(target), (f"[dry-run] {delogo_filter(x, y, w, h)}"
                                   f"（源 {width}x{height}）")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(source), "-vf", delogo_filter(x, y, w, h),
           "-c:a", "copy", str(target)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=1800,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", f"ffmpeg 执行失败：{type(exc).__name__}: {exc}"
    if proc.returncode != 0 or not target.is_file():
        return False, "", f"ffmpeg 返回 {proc.returncode}：{(proc.stderr or '')[-300:]}"
    return True, str(target), (f"已擦除水印区域 {w}x{h} @ ({x},{y})"
                               f"（源 {width}x{height}，有损处理）")
