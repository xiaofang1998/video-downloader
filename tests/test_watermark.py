"""去水印相关测试：无水印源挑选、delogo 几何与真机执行、订单级选项接线。

两层要分清（这也是产品里的两层）：
* **取无水印源**（无损）—— 平台同时给带水印/无水印两个地址时的挑选与标注；
* **像素级擦除**（有损）—— ffmpeg delogo，只在用户明确要求时用。
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from xydl import watermark
from xydl.config import Config
from xydl.downloader import Downloader
from xydl.models import DownloadResult


# ── 无水印源挑选 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v1", True),
    ("https://aweme.snssdk.com/aweme/v1/play/?video_id=v1", False),
    ("https://x/video.mp4?watermark=1", True),
    ("https://x/video.mp4?watermark=0", False),
    ("https://x/video.mp4", False),
    ("", False),
])
def test_looks_watermarked(url, expected):
    assert watermark.looks_watermarked(url) is expected


def test_clean_variants_rewrites_known_markers():
    variants = watermark.clean_variants(
        "https://aweme/v1/playwm/?video_id=v1&watermark=1")
    assert any("/play/" in v for v in variants), variants
    assert any(v.endswith("watermark=0") for v in variants), variants


def test_order_candidates_prefers_clean_url():
    """带水印的地址要排到后面，改写出的干净版本排前面。"""
    watermarked = "https://aweme/v1/playwm/?video_id=v1"
    clean = "https://aweme/v1/play/?video_id=v1"
    ordered = watermark.order_candidates([watermarked, clean])
    assert ordered[0] == clean
    assert watermarked in ordered, "带水印的原地址要留作兜底"

    # any 模式（不挑）时保持原顺序
    as_is = watermark.order_candidates([watermarked, clean], prefer_no_watermark=False)
    assert as_is[0] == watermarked


def test_pick_source_prefers_clean_and_keeps_watermarked_last():
    """只有一个带水印地址时：先给改写出的干净版（首选），带水印原地址留作兜底。

    说明：``playwm`` → ``play`` 是**改写**出来的候选，平台不保证存在，所以
    真正"按顺序试、失败再退"的逻辑在 ``Downloader._try_candidates`` 里
    （见下面 watermark=off / auto / any 三个用例）。
    """
    only = "https://aweme/v1/playwm/?video_id=v1"
    ordered = watermark.order_candidates([only])
    assert ordered[0] == "https://aweme/v1/play/?video_id=v1", "干净版排第一"
    assert ordered[-1] == only, "带水印原地址兜底排最后"

    url, note = watermark.pick_source(["https://aweme/v1/play/?video_id=v1"])
    assert note == "", "本来就是干净源，不用特别标注"


# ── delogo 几何 ───────────────────────────────────────────────────────
def test_rect_for_platform_preset_is_bottom_right():
    x, y, w, h = watermark.rect_for("douyin", 1080, 1920)
    assert w > 0 and h > 0
    assert x + w <= 1080 and y + h <= 1920, "水印矩形必须在画面内"
    assert y > 1920 * 0.8, "预设是右下角水印"


def test_rect_for_explicit_rect_wins():
    assert watermark.rect_for("douyin", 1080, 1920, rect="10:20:30:40") == (10, 20, 30, 40)
    assert watermark.rect_for("", 0, 0, rect="10,20,30,40") == (10, 20, 30, 40)


@pytest.mark.parametrize("platform,rect,fragment", [
    ("unknowner", "", "不认识平台"),
    ("", "", "不认识平台"),
    ("douyin", "1:2:3", "x:y:w:h"),
    ("douyin", "a:b:c:d", "数字"),
    ("douyin", "1:2:0:4", "大于 0"),
])
def test_rect_for_errors_are_actionable(platform, rect, fragment):
    """参数不对要说清怎么改，不能静默盖错地方。"""
    with pytest.raises(ValueError) as info:
        watermark.rect_for(platform, 1080, 1920, rect=rect)
    assert fragment in str(info.value)


def test_guess_platform_from_url():
    assert watermark.guess_platform("https://v.douyin.com/x") == "douyin"
    assert watermark.guess_platform("https://v.kuaishou.com/x") == "kuaishou"
    assert watermark.guess_platform("https://weixin.qq.com/sph/x") == "wechat_channels"
    assert watermark.guess_platform("https://www.bilibili.com/video/BV1") == ""


# ── delogo 真机执行（ffmpeg 造带"假水印"的视频）────────────────────
def _ffmpeg() -> str:
    path = Downloader(Config({})).ytdlp.find_ffmpeg()
    if not path:
        pytest.skip("没有 ffmpeg，跳过 delogo 真机测试")
    return path


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def test_delogo_file_changes_only_that_region(tmp_path: Path):
    """真机跑一遍：水印区域像素要变，分辨率/时长不能变（有损但要可控）。"""
    ffmpeg = _ffmpeg()
    src = tmp_path / "wm.mp4"
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
          "-i", "testsrc=size=640x360:rate=10:duration=2", "-vf",
          "drawbox=x=500:y=300:w=120:h=40:color=white@1.0:t=fill",
          "-pix_fmt", "yuv420p", str(src)])

    ok, out, message = watermark.delogo_file(ffmpeg, src, rect="500:300:120:40")
    assert ok, message
    assert Path(out).is_file()
    assert "有损" in message, "要说清这是有损处理"

    def region_hash(path: Path) -> str:
        png = path.with_suffix(".png")
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
              "-vf", "crop=120:40:500:300", "-frames:v", "1", str(png)])
        return hashlib.sha256(png.read_bytes()).hexdigest()

    assert region_hash(src) != region_hash(Path(out)), "水印区域必须真的被改过"

    info = subprocess.run([ffmpeg, "-hide_banner", "-i", out], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stderr
    assert re.search(r"640x360", info), "分辨率不能被改"
    assert re.search(r"Duration: 00:00:02", info), "时长不能被改"
    assert out.endswith(".nowm.mp4"), "默认另存，不覆盖源文件（有损处理要能回退）"


def test_delogo_dry_run_does_not_write(tmp_path: Path):
    ffmpeg = _ffmpeg()
    src = tmp_path / "s.mp4"
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
          "-i", "testsrc=size=320x240:rate=5:duration=1", str(src)])
    ok, target, message = watermark.delogo_file(ffmpeg, src, platform="douyin",
                                                dry_run=True)
    assert ok and "[dry-run]" in message
    assert not Path(target).exists(), "dry-run 不能写文件"


# ── 订单级选项接线 ────────────────────────────────────────────────────
#: 模拟"干净版地址不存在"：带水印的 playwm 能下，改写成 play 的 404。
#: 这是真实情况 —— 无水印地址是改写来的，平台不保证存在，所以必须能退回。
def _douyin_setup(monkeypatch, tmp_path: Path, downloader: Downloader) -> list[str]:
    import xydl.cdp_fetch as cdp

    monkeypatch.setattr(cdp, "login_profile_dir", lambda _p: tmp_path / "prof")
    (tmp_path / "prof").mkdir(exist_ok=True)
    monkeypatch.setattr(cdp, "fetch_douyin_video",
                        lambda _u, _p: ("https://aweme/v1/playwm/?video_id=v1", "标题"))
    monkeypatch.setattr(downloader.ytdlp, "_clean_job_dir", lambda _p: None)
    return []


class _HttpStub:
    """干净版（/play/）一定 404，带水印版（playwm）能下。"""

    def __init__(self, tmp_path: Path, seen: list[str]):
        self.tmp_path = tmp_path
        self.seen = seen

    def download(self, url, job_dir, **kwargs):
        self.seen.append(url)
        if watermark.looks_watermarked(url):
            return DownloadResult(True, engine="http",
                                  file_path=str(self.tmp_path / "wm.mp4"))
        return DownloadResult(False, engine="http", error="404")


def test_download_refuses_watermarked_when_off(monkeypatch, tmp_path: Path):
    """watermark=off：干净版拿不到时**不下带水印的**，并说明原因。"""
    config = Config({})
    config.set("download.prefer_ytdlp", False)
    downloader = Downloader(config)
    _douyin_setup(monkeypatch, tmp_path, downloader)
    seen: list[str] = []
    monkeypatch.setattr(downloader, "http", _HttpStub(tmp_path, seen))

    result = downloader.download("https://v.douyin.com/x/", tmp_path,
                                 watermark="off")
    assert not result.ok
    assert all(not watermark.looks_watermarked(u) for u in seen), \
        "off 模式不该去下带水印的源"
    assert "带水印" in result.error


def test_download_accepts_watermarked_when_any(monkeypatch, tmp_path: Path):
    """watermark=any：不挑，拿到哪个算哪个。"""
    config = Config({})
    config.set("download.prefer_ytdlp", False)
    downloader = Downloader(config)
    _douyin_setup(monkeypatch, tmp_path, downloader)
    seen: list[str] = []
    monkeypatch.setattr(downloader, "http", _HttpStub(tmp_path, seen))

    result = downloader.download("https://v.douyin.com/x/", tmp_path,
                                 watermark="any")
    assert result.ok
    assert seen and watermark.looks_watermarked(seen[0]), "any 模式按原样用"
    assert not result.meta.get("watermark"), "any 模式不标注"


def test_download_falls_back_to_watermarked_and_marks_it(monkeypatch, tmp_path: Path):
    """auto（默认）：先试干净版，失败**自动退回**带水印版，并如实标注。"""
    config = Config({})
    config.set("download.prefer_ytdlp", False)
    downloader = Downloader(config)
    _douyin_setup(monkeypatch, tmp_path, downloader)
    seen: list[str] = []
    monkeypatch.setattr(downloader, "http", _HttpStub(tmp_path, seen))

    result = downloader.download("https://v.douyin.com/x/", tmp_path)
    assert result.ok
    assert len(seen) >= 2, f"应先试干净版再退回：{seen}"
    assert not watermark.looks_watermarked(seen[0])
    assert watermark.looks_watermarked(seen[-1])
    assert result.meta.get("watermark") == "带水印源", \
        "用了带水印源就必须标注（交付话术要能说清）"


def test_download_delogo_runs_when_only_watermarked(monkeypatch, tmp_path: Path):
    """watermark=delogo：只有带水印版本时，下完自动用 ffmpeg 擦掉。"""
    config = Config({})
    config.set("download.prefer_ytdlp", False)
    downloader = Downloader(config)
    _douyin_setup(monkeypatch, tmp_path, downloader)
    seen: list[str] = []
    monkeypatch.setattr(downloader, "http", _HttpStub(tmp_path, seen))

    called: dict = {}

    def fake_delogo(ffmpeg, src, **kwargs):
        called["src"] = str(src)
        called["platform"] = kwargs.get("platform")
        return True, str(src).replace(".mp4", ".nowm.mp4"), "已擦除（有损处理）"

    monkeypatch.setattr(downloader.ytdlp, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr("xydl.downloader.watermark_mod.delogo_file", fake_delogo)

    result = downloader.download("https://v.douyin.com/x/", tmp_path,
                                 watermark="delogo")
    assert result.ok
    assert called, "delogo 模式必须调用擦除"
    assert called["platform"] == "douyin", "要按平台预设定位水印"
    assert result.file_path.endswith(".nowm.mp4")
    assert "watermark" not in result.meta, "擦过之后不该再标'带水印源'"
    assert "delogo" in result.meta


def test_format_chains_honours_quality_override():
    """清晰度按订单覆盖：1080 的过滤器里要出现 height<=1080。"""
    config = Config({})
    config.set("download.quality", "best")
    engine = Downloader(config).ytdlp
    chains = engine._format_chains("1080")
    flat = " ".join(" ".join(chain) for chain in chains)
    assert "height<=1080" in flat
    assert "height<=1080" not in " ".join(
        " ".join(c) for c in engine._format_chains("best"))


def test_pipeline_download_options_reads_order_meta(monkeypatch, config, store):
    """订单 meta 里的 quality/watermark 要变成下载参数；没填就不传（用配置）。

    用假订单对象直接测这个映射：真起订单会拉工作线程，测的是同一段逻辑但更脆。
    """
    from xydl.pipeline import Pipeline

    pipe = Pipeline(config, store)          # 不 start()，避免起工作线程

    class WithMeta:
        meta = {"quality": "720", "watermark": "delogo"}

    monkeypatch.setattr(store, "get", lambda _id: WithMeta())
    assert pipe._download_options(1) == {"quality": "720", "watermark": "delogo"}

    class Empty:
        meta = {}

    monkeypatch.setattr(store, "get", lambda _id: Empty())
    assert pipe._download_options(1) == {}, "没填就用配置默认，不传空值"

    monkeypatch.setattr(store, "get", lambda _id: None)
    assert pipe._download_options(1) == {}, "订单不存在也不能炸"
