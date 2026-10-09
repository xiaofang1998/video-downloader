"""下载引擎测试 —— 全部打本地 HTTP 服务器，离线可跑。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xydl.config import Config
from xydl.downloader import Downloader, HttpEngine, Progress, job_dir_for
from xydl.utils import (
    clean_media_title,
    ensure_unique,
    is_generic_title,
    pick_best_title,
    sanitize_filename,
)
from conftest import SAMPLE_BYTES


@pytest.fixture
def engine(config: Config) -> HttpEngine:
    eng = HttpEngine(config)
    eng.proxy = ""      # 本地回环必须直连
    return eng


def test_download_direct_mp4(engine: HttpEngine, sample_server, tmp_path: Path):
    seen: list[Progress] = []
    result = engine.download(sample_server.url("sample.mp4"), tmp_path / "job",
                             progress=seen.append)
    assert result.ok, result.error
    assert result.size_bytes == len(SAMPLE_BYTES)
    assert Path(result.file_path).read_bytes() == SAMPLE_BYTES
    assert seen and seen[-1].percent == 100.0


def test_download_rejects_html_page(engine: HttpEngine, sample_server, tmp_path: Path):
    """把网页当视频存下来是最隐蔽的 bug 之一，必须拦住。"""
    job = tmp_path / "job"
    result = engine.download(sample_server.url("page.html"), job)
    assert not result.ok
    assert "网页" in result.error
    assert not list(job.glob("*"))           # 不该留下垃圾文件


def test_download_404_fails_cleanly(engine: HttpEngine, sample_server, tmp_path: Path):
    result = engine.download(sample_server.url("nope.mp4"), tmp_path / "job")
    assert not result.ok
    assert result.error


def test_download_respects_max_filesize(config: Config, sample_server, tmp_path: Path):
    config.set("download.max_filesize_mb", 1)     # 样本是 2 MiB
    engine = HttpEngine(config)
    engine.proxy = ""
    result = engine.download(sample_server.url("sample.mp4"), tmp_path / "job")
    assert not result.ok
    assert "上限" in result.error


def test_download_can_be_cancelled(config: Config, sample_server, tmp_path: Path):
    import threading

    engine = HttpEngine(config)
    engine.proxy = ""
    cancel = threading.Event()
    cancel.set()                                   # 一开始就取消
    result = engine.download(sample_server.url("sample.mp4"), tmp_path / "job",
                             cancel=cancel)
    assert not result.ok
    assert "取消" in result.error


def test_facade_falls_back_to_http_when_ytdlp_disabled(config: Config,
                                                       sample_server, tmp_path: Path):
    config.set("download.prefer_ytdlp", False)
    downloader = Downloader(config)
    result = downloader.download(sample_server.url("sample.mp4"), tmp_path / "job")
    assert result.ok, result.error
    assert result.engine == "http"


def test_facade_reports_why_it_failed(config: Config, sample_server, tmp_path: Path):
    config.set("download.prefer_ytdlp", False)
    downloader = Downloader(config)
    result = downloader.download(sample_server.url("page.html"), tmp_path / "job")
    assert not result.ok
    assert result.error    # 必须给人话，而不是空字符串


def test_job_dir_is_isolated_per_order(tmp_path: Path):
    a = job_dir_for(tmp_path, 1)
    b = job_dir_for(tmp_path, 2)
    assert a != b and a.exists() and b.exists()


def test_locate_output_picks_largest(tmp_path: Path):
    from xydl.downloader import YtDlpEngine

    job = tmp_path / "job"
    job.mkdir()
    (job / "thumb.jpg").write_bytes(b"x" * 10)
    (job / "video.mp4").write_bytes(b"y" * 5000)
    (job / "video.mp4.part").write_bytes(b"z" * 99999)   # 残留分片要排除
    assert YtDlpEngine._locate_output(job, "").name == "video.mp4"


def test_locate_output_prefers_hint(tmp_path: Path):
    from xydl.downloader import YtDlpEngine

    job = tmp_path / "job"
    job.mkdir()
    (job / "big.mp4").write_bytes(b"y" * 5000)
    (job / "wanted.mp4").write_bytes(b"z" * 10)
    assert YtDlpEngine._locate_output(job, str(job / "wanted.mp4")).name == "wanted.mp4"


def test_locate_output_returns_none_when_empty(tmp_path: Path):
    from xydl.downloader import YtDlpEngine

    job = tmp_path / "job"
    job.mkdir()
    (job / "half.mp4.part").write_bytes(b"z" * 999)
    assert YtDlpEngine._locate_output(job, "") is None


# ── DASH 分片绝不能被当成成品 ─────────────────────────────────────────
def test_dash_fragments_are_never_treated_as_output(tmp_path: Path):
    """真实事故：没合流成功时把 .m4a 音频轨当成品交付给客户。"""
    from xydl.downloader import YtDlpEngine

    job = tmp_path / "job"
    job.mkdir()
    (job / "标题.f100026.mp4").write_bytes(b"v" * 15_000)
    (job / "标题.f30280.m4a").write_bytes(b"a" * 5_000)
    assert YtDlpEngine._locate_output(job, "") is None
    assert len(YtDlpEngine._fragments(job)) == 2


def test_dash_fragment_hint_is_rejected(tmp_path: Path):
    """即使 yt-dlp 报的路径是分片，也不能采信。"""
    from xydl.downloader import YtDlpEngine

    job = tmp_path / "job"
    job.mkdir()
    frag = job / "标题.f30280.m4a"
    frag.write_bytes(b"a" * 5_000)
    assert YtDlpEngine._locate_output(job, str(frag)) is None


def test_merged_output_wins_over_leftover_fragments(tmp_path: Path):
    from xydl.downloader import YtDlpEngine

    job = tmp_path / "job"
    job.mkdir()
    (job / "标题.f100026.mp4").write_bytes(b"v" * 15_000)
    (job / "标题.f30280.m4a").write_bytes(b"a" * 5_000)
    (job / "标题.mp4").write_bytes(b"m" * 20_000)
    assert YtDlpEngine._locate_output(job, "").name == "标题.mp4"


def test_fragment_explanation_mentions_ffmpeg(config: Config, tmp_path: Path):
    from xydl.downloader import YtDlpEngine

    engine = YtDlpEngine(config)
    engine._ffmpeg_resolved = ""            # 模拟找不到 ffmpeg
    job = tmp_path / "job"
    job.mkdir()
    (job / "x.f100026.mp4").write_bytes(b"v" * 100)
    message = engine._explain_download_failure(0, [], job)
    assert "ffmpeg" in message and "音视频轨" in message


def test_fragment_explanation_with_ffmpeg_points_at_config(config: Config, tmp_path: Path):
    from xydl.downloader import YtDlpEngine

    engine = YtDlpEngine(config)
    engine._ffmpeg_resolved = r"C:\fake\ffmpeg.exe"    # 假装有 ffmpeg
    job = tmp_path / "job"
    job.mkdir()
    (job / "x.f100026.mp4").write_bytes(b"v" * 100)
    assert "合流失败" in engine._explain_download_failure(0, [], job)


def test_probe_ffmpeg_rejects_broken_binary(config: Config, tmp_path: Path):
    """必须实测：mise 的 shim 存在但跑不起来（0xC0000135），交给 yt-dlp 会
    导致「退出码 0 但没合流」这种最难查的静默失败。"""
    from xydl.downloader import YtDlpEngine

    engine = YtDlpEngine(config)
    assert engine.probe_ffmpeg("") is False
    assert engine.probe_ffmpeg(str(tmp_path / "不存在.exe")) is False

    bogus = tmp_path / "ffmpeg.exe"
    bogus.write_bytes(b"not a real executable")
    assert engine.probe_ffmpeg(str(bogus)) is False


def test_find_ffmpeg_skips_broken_candidate(config: Config, tmp_path: Path,
                                            monkeypatch):
    from xydl.downloader import YtDlpEngine

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "ffmpeg.exe").write_bytes(b"junk")
    good = tmp_path / "good"
    good.mkdir()
    good_exe = good / "ffmpeg.exe"
    good_exe.write_bytes(b"junk")

    engine = YtDlpEngine(config)
    engine._ffmpeg_candidates = lambda: [str(broken / "ffmpeg.exe"), str(good_exe)]
    engine.probe_ffmpeg = lambda p: p == str(good_exe)   # type: ignore[method-assign]
    assert engine.find_ffmpeg() == str(good_exe)


def test_find_ffmpeg_caches_result(config: Config, tmp_path: Path):
    from xydl.downloader import YtDlpEngine

    calls: list[str] = []

    engine = YtDlpEngine(config)
    engine._ffmpeg_candidates = lambda: [str(tmp_path / "a")]
    engine.probe_ffmpeg = lambda p: calls.append(p) or False   # type: ignore[method-assign]
    assert engine.find_ffmpeg() == ""
    assert engine.find_ffmpeg() == ""       # 第二次走缓存
    assert len(calls) == 1


def test_cookie_args_prefers_file_over_browser(config: Config, tmp_path: Path):
    """cookies_file 优先；文件不存在时不该传一个无效路径给 yt-dlp。"""
    from xydl.downloader import YtDlpEngine

    engine = YtDlpEngine(config)
    assert engine._cookie_args() == []

    engine.cookies_from_browser = "edge"
    assert engine._cookie_args() == ["--cookies-from-browser", "edge"]

    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    engine.cookies_file = str(jar)
    assert engine._cookie_args() == ["--cookies", str(jar)]

    engine.cookies_file = str(tmp_path / "nope.txt")   # 不存在
    assert engine._cookie_args() == ["--cookies-from-browser", "edge"]  # 回落


def test_douyin_cookie_error_is_translated():
    """抖音/西瓜的 'cookies are needed' 必须变成可操作的提示。"""
    from xydl.downloader import YtDlpEngine

    for raw in (
        "ERROR: [Douyin] 7689741577656339766: Fresh cookies (not necessarily logged in) are needed",
        "ERROR: [Ixigua] 7300000000000000000: Cookies (not necessarily logged in) are needed",
    ):
        message = YtDlpEngine._explain(1, [raw])
        assert "cookie" in message.lower()
        assert "login" in message                 # 告诉用户跑哪条命令
        assert "download.cookies_file" in message  # 以及备选方案


def test_youtube_bot_error_tells_you_not_to_send_cookies():
    from xydl.downloader import YtDlpEngine

    message = YtDlpEngine._explain(
        1, ["ERROR: [youtube] dQw4w9WgXcQ: The page needs to be reloaded."])
    assert "不要给它带 cookie" in message or "cookie_mode" in message


# ── cookie 按域名下发 ────────────────────────────────────────────────
def make_cookie_engine(config: Config, *, browser: str = "chrome:C:/x",
                       mode: str | None = None, domains=None):
    from xydl.downloader import YtDlpEngine

    config.set("download.cookies_from_browser", browser)
    if mode is not None:
        config.set("download.cookie_mode", mode)
    if domains is not None:
        config.set("download.cookie_domains", domains)
    return YtDlpEngine(config)


@pytest.mark.parametrize("url,wanted", [
    # 需要 cookie 的站点
    ("https://v.douyin.com/abc/", True),
    ("https://www.douyin.com/video/1", True),
    ("https://www.xiaohongshu.com/explore/1", True),
    ("https://www.ixigua.com/730", True),
    ("https://video.weibo.com/show?fid=1", True),
    # 不需要、而且给了反而会坏的站点
    ("https://youtu.be/dQw4w9WgXcQ", False),
    ("https://www.youtube.com/watch?v=x", False),
    ("https://www.tiktok.com/@a/video/1", False),
    ("https://x.com/u/status/1", False),
    ("https://www.instagram.com/reel/x/", False),
])
def test_cookie_policy_is_per_domain(config: Config, url, wanted):
    """cookie 是站点特定的：YouTube 带了别的会话的 cookie 会被风控拒绝。"""
    assert make_cookie_engine(config)._cookies_wanted(url) is wanted


def test_cookie_mode_always_and_never(config: Config):
    assert make_cookie_engine(config, mode="always")._cookies_wanted(
        "https://youtu.be/x") is True
    assert make_cookie_engine(config, mode="never")._cookies_wanted(
        "https://v.douyin.com/x/") is False


def test_cookie_domains_are_suffix_matched(config: Config):
    engine = make_cookie_engine(config, domains=["douyin.com"])
    assert engine._cookies_wanted("https://v.douyin.com/x/") is True
    assert engine._cookies_wanted("https://www.douyin.com/video/1") is True
    assert engine._cookies_wanted("https://evil-douyin.com/x") is False
    assert engine._cookies_wanted("https://notdouyin.com/x") is False


def test_empty_cookie_domains_falls_back_to_sending(config: Config):
    """白名单为空时退回老行为，免得用户配了就"什么都没带"却不知道为什么。"""
    engine = make_cookie_engine(config, domains=[])
    assert engine._cookies_wanted("https://youtu.be/x") is True


def test_no_cookies_configured_means_no_args(config: Config):
    from xydl.downloader import YtDlpEngine

    engine = YtDlpEngine(config)
    engine.cookies_from_browser = ""
    engine.cookies_file = ""
    assert engine._cookies_wanted("https://v.douyin.com/x/") is False
    assert engine._cookie_args("https://v.douyin.com/x/") == []


def test_cookie_args_respects_domain_gating(config: Config):
    engine = make_cookie_engine(config, browser="chrome:C:/x")
    assert engine._cookie_args("https://v.douyin.com/x/") == \
        ["--cookies-from-browser", "chrome:C:/x"]
    assert engine._cookie_args("https://youtu.be/x") == []


def test_common_args_wire_cookie_gating(config: Config):
    engine = make_cookie_engine(config, browser="chrome:C:/x")
    assert "--cookies-from-browser" in engine._common_args("https://v.douyin.com/x/")
    assert "--cookies-from-browser" not in engine._common_args("https://youtu.be/x")


def test_child_env_restores_missing_windows_vars(monkeypatch):
    """yt-dlp 靠 LOCALAPPDATA/APPDATA 定位浏览器 cookie 库。

    被裁剪过的宿主环境（服务进程、沙箱）可能不提供它们，会让
    --cookies-from-browser 报「找不到 cookie 数据库」。必须补上。
    """
    from xydl.downloader import YtDlpEngine

    for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        monkeypatch.delenv(key, raising=False)
    env = YtDlpEngine._child_env()
    for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        assert env.get(key), f"{key} 应该被补齐"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"


def test_child_env_does_not_clobber_existing_values(monkeypatch):
    from xydl.downloader import YtDlpEngine

    monkeypatch.setenv("LOCALAPPDATA", r"D:\custom\Local")
    assert YtDlpEngine._child_env()["LOCALAPPDATA"] == r"D:\custom\Local"


def test_child_env_treats_empty_value_as_missing(monkeypatch):
    from xydl.downloader import YtDlpEngine

    monkeypatch.setenv("LOCALAPPDATA", "")
    assert YtDlpEngine._child_env()["LOCALAPPDATA"]


def test_detect_browsers_returns_a_list():
    from xydl.downloader import YtDlpEngine

    found = YtDlpEngine.detect_browsers()
    assert isinstance(found, list)
    assert all(isinstance(name, str) for name in found)
    # 名字必须是 yt-dlp 认识的取值
    assert set(found) <= {"edge", "chrome", "firefox", "brave", "vivaldi", "chromium"}


def test_locked_cookie_database_has_actionable_hint():
    from xydl.downloader import YtDlpEngine

    message = YtDlpEngine._explain(1, ["ERROR: Could not copy Chrome cookie database."])
    assert "关闭" in message or "关掉" in message


def test_search_dirs_works_without_appdata(monkeypatch):
    """APPDATA 缺失时（某些沙箱/服务进程）不能抛异常，要用 Path.home() 兜底。"""
    from xydl.downloader import YtDlpEngine

    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    dirs = YtDlpEngine._ffmpeg_search_dirs()
    assert dirs and all(isinstance(d, Path) for d in dirs)


def test_explain_translates_ytdlp_errors():
    from xydl.downloader import YtDlpEngine
    assert "不认识" in YtDlpEngine._explain(1, ["ERROR: Unsupported URL: xyz"])
    assert "登录" in YtDlpEngine._explain(1, ["ERROR: Sign in to confirm your age"])
    assert "ffmpeg" in YtDlpEngine._explain(
        1, ["ERROR: You have requested merging of multiple formats but ffmpeg is not installed"]
    )
    assert "yt-dlp 退出码 7" in YtDlpEngine._explain(7, [])


def test_format_chains_drop_merge_without_ffmpeg(config: Config):
    """没有 ffmpeg 就不能要「视频轨 + 音频轨」的合并格式，否则必然失败。"""
    from xydl.downloader import YtDlpEngine

    engine = YtDlpEngine(config)
    engine.has_ffmpeg = lambda: False          # type: ignore[method-assign]
    joined = " ".join(" ".join(c) for c in engine._format_chains())
    assert "bv*+ba" not in joined
    assert "--merge-output-format" not in joined
    assert "b[ext=mp4]/b" in joined

    engine.has_ffmpeg = lambda: True           # type: ignore[method-assign]
    joined = " ".join(" ".join(c) for c in engine._format_chains())
    assert "--merge-output-format" in joined
    assert "bv*+ba" in joined


def test_format_chains_always_have_fallbacks(config: Config):
    """单一选择器太脆：站点只有 DASH 分离流时会直接失败，必须有备选。"""
    from xydl.downloader import YtDlpEngine

    engine = YtDlpEngine(config)
    for merge in (True, False):
        engine.has_ffmpeg = lambda m=merge: m        # type: ignore[method-assign]
        assert len(engine._format_chains()) >= 2


def test_format_chains_honour_quality(config: Config):
    from xydl.downloader import YtDlpEngine

    config.set("download.quality", "720")
    config.set("download.prefer_compatible", False)
    engine = YtDlpEngine(config)
    engine.has_ffmpeg = lambda: True           # type: ignore[method-assign]
    assert "height<=720" in engine._format_chains()[0][1]


def test_format_chains_prefer_h264_by_default(config: Config):
    """交付给客户的文件必须到处都能播。

    YouTube 默认给 AV1 + Opus，老手机/剪映打不开，所以默认优先 H.264 + AAC。
    """
    from xydl.downloader import YtDlpEngine

    engine = YtDlpEngine(config)
    engine.has_ffmpeg = lambda: True           # type: ignore[method-assign]
    first = engine._format_chains()[0][1]
    assert "vcodec^=avc1" in first
    assert "acodec^=mp4a" in first


def test_format_chains_can_be_asked_for_best_codec(config: Config):
    from xydl.downloader import YtDlpEngine

    config.set("download.prefer_compatible", False)
    engine = YtDlpEngine(config)
    engine.has_ffmpeg = lambda: True           # type: ignore[method-assign]
    assert "avc1" not in engine._format_chains()[0][1]


def test_compatible_chain_still_has_fallbacks(config: Config):
    """站点没有 H.264 时必须能退回去，不能因为偏好把下载搞失败。"""
    from xydl.downloader import YtDlpEngine

    engine = YtDlpEngine(config)
    engine.has_ffmpeg = lambda: True           # type: ignore[method-assign]
    chains = engine._format_chains()
    assert len(chains) >= 3
    assert any("avc1" not in c[1] for c in chains)     # 有不含 avc1 的兜底档


def test_format_retryable_classification():
    from xydl.downloader import _FORMAT_RETRYABLE
    assert _FORMAT_RETRYABLE.search("ERROR: Requested format is not available")
    assert not _FORMAT_RETRYABLE.search("ERROR: Video unavailable")
    assert not _FORMAT_RETRYABLE.search("HTTP Error 403: Forbidden")


def test_clean_job_dir_removes_partial_downloads(tmp_path: Path):
    from xydl.downloader import YtDlpEngine

    job = tmp_path / "job"
    job.mkdir()
    (job / "half.mp4").write_bytes(b"x")
    (job / "full.mp4").write_bytes(b"y")
    YtDlpEngine._clean_job_dir(job)
    assert list(job.iterdir()) == []


def test_find_ffmpeg_prefers_configured_file(config: Config, tmp_path: Path):
    """配置了可用的 ffmpeg 就用它（用 monkeypatch 保证不依赖真实环境）。"""
    from xydl.downloader import YtDlpEngine

    configured = tmp_path / "my-ffmpeg.exe"
    configured.write_bytes(b"stub")
    config.set("download.ffmpeg_location", str(configured))

    engine = YtDlpEngine(config)
    engine.probe_ffmpeg = lambda p: p == str(configured)   # type: ignore[method-assign]
    assert engine.find_ffmpeg() == str(configured)


def test_find_ffmpeg_accepts_configured_directory(config: Config, tmp_path: Path):
    from xydl.downloader import YtDlpEngine

    folder = tmp_path / "ffmpeg-bin"
    folder.mkdir()
    exe = folder / "ffmpeg.exe"
    exe.write_bytes(b"stub")
    config.set("download.ffmpeg_location", str(folder))

    engine = YtDlpEngine(config)
    engine.probe_ffmpeg = lambda p: p == str(exe)          # type: ignore[method-assign]
    assert engine.find_ffmpeg() == str(exe)


def test_broken_configured_ffmpeg_is_not_trusted(config: Config, tmp_path: Path):
    """配置了一个跑不起来的路径时必须继续往下找。盲信它会导致
    yt-dlp「退出码 0 但没合流」这种静默失败。"""
    from xydl.downloader import YtDlpEngine

    broken = tmp_path / "broken.exe"
    broken.write_bytes(b"junk")
    good = tmp_path / "good.exe"
    good.write_bytes(b"junk")
    config.set("download.ffmpeg_location", str(broken))

    engine = YtDlpEngine(config)
    engine._ffmpeg_candidates = lambda: [str(broken), str(good)]
    engine.probe_ffmpeg = lambda p: p == str(good)         # type: ignore[method-assign]
    assert engine.find_ffmpeg() == str(good)               # 跳过坏的，选中好的


# ── 文件名清洗 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("猫咪/打呼噜:合集?", "猫咪_打呼噜_合集"),
    ("a<b>c|d*e", "a_b_c_d_e"),
    ("trailing dots...", "trailing dots"),
    ("   ", "video"),
    ("", "video"),
    ("CON", "_CON"),
    ("com1", "_com1"),
])
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


# ── 平台元数据标题的收拾 ──────────────────────────────────────────────
def test_clean_media_title_takes_first_line():
    """真实案例：抖音 title = 一行标题 + 换行 + 整段简介。

    不处理的话文件名会变成 66 个字符、塞满简介和《》@ 的一长串。
    """
    raw = ("吹笛子建议收藏🌟这首拿去商演真的夯爆了 《韩湘子秘谱》@钟世祺  \n"
           "这是一首融合竹笛与爵士律动的跨界作品。保留竹笛清亮、飘逸的东方音色，同时")
    cleaned = clean_media_title(raw)
    assert "\n" not in cleaned
    assert "这是一首融合" not in cleaned          # 简介被丢掉
    assert cleaned.startswith("吹笛子建议收藏")


def test_clean_media_title_truncates():
    assert len(clean_media_title("长" * 200, maxlen=40)) == 40


def test_clean_media_title_empty_variants():
    assert clean_media_title("") == ""
    assert clean_media_title("   \n  \n ") == ""
    assert clean_media_title(None) == ""


def test_clean_media_title_strips_junk_at_cut_point():
    """截断点正好落在标点上时，标点要被削掉（否则文件名以「。」结尾很难看）。"""
    assert clean_media_title("猫咪打呼噜合集。", maxlen=8) == "猫咪打呼噜合集"
    assert clean_media_title("猫咪打呼噜合集···", maxlen=9) == "猫咪打呼噜合集"


def test_clean_media_title_leaves_short_titles_alone():
    assert clean_media_title("猫咪打呼噜合集") == "猫咪打呼噜合集"


# ── 占位标题判别 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("title,generic", [
    ("", True),
    ("   ", True),
    ("video", True),
    ("Untitled", True),
    # 实测：小红书的 yt-dlp 元数据标题长这样
    ("XiaoHongShu video #6ab896ff0000000018007f59", True),
    ("6ab896ff0000000018007f59", True),
    ("video #abcdef1234", True),
    # 正常标题不该被误判
    ("当你开始注重 你将拥有紧致有型的身材", False),
    ("【官方 MV】Never Gonna Give You Up - Rick Astley", False),
    ("猫咪打呼噜合集", False),
    ("吹笛子建议收藏这首拿去商演真的夯爆了", False),
])
def test_is_generic_title(title, generic):
    assert is_generic_title(title) is generic


def test_pick_best_title_prefers_metadata_when_it_is_real():
    assert pick_best_title("猫咪打呼噜合集", "买家随便打的") == "猫咪打呼噜合集"


def test_pick_best_title_falls_back_to_share_text_when_metadata_is_generic():
    """小红书那条的真实情况：元数据是占位名，分享文案才是真标题。"""
    assert pick_best_title(
        "XiaoHongShu video #6ab896ff0000000018007f59",
        "当你开始注重 你将拥有紧致有型的身材",
    ) == "当你开始注重 你将拥有紧致有型的身材"


def test_pick_best_title_falls_back_to_metadata_when_both_are_generic():
    assert pick_best_title("video #abcdef1234", "") == "video #abcdef1234"


def test_pick_best_title_handles_empty():
    assert pick_best_title("", "") == ""


def test_sanitize_filename_truncates():
    assert len(sanitize_filename("き" * 300, maxlen=50)) == 50


def test_sanitize_strips_emoji():
    assert sanitize_filename("视频🎬好看") == "视频好看"


def test_ensure_unique(tmp_path: Path):
    target = tmp_path / "a.mp4"
    target.write_bytes(b"1")
    assert ensure_unique(target).name == "a-1.mp4"
    (tmp_path / "a-1.mp4").write_bytes(b"2")
    assert ensure_unique(target).name == "a-2.mp4"
    assert ensure_unique(tmp_path / "new.mp4").name == "new.mp4"


def test_download_clears_stale_files_before_running(config: Config, tmp_path: Path,
                                                    monkeypatch):
    """★ 回归：任务目录里上次残留的成品，不能被误判成本次下载结果。

    真踩过：``run.py download`` 复用同一个 job_dir，抖音下载失败（缺 cookie）
    时，``_locate_output`` 的「取最大文件」兜底把上一次 YouTube 留下的
    Bow.mp4 当成这次的成功 —— 文件名、大小全是上个任务的，还报「✅ 成功」。
    """
    from xydl.downloader import DownloadResult

    job = tmp_path / "job"
    job.mkdir()
    stale = job / "old.mp4"
    stale.write_bytes(b"x" * 100)

    dl = Downloader(config)

    def _fail(*_a, **_k):
        return DownloadResult(False, engine="", error="下载失败")

    monkeypatch.setattr(dl.ytdlp, "download", _fail)
    monkeypatch.setattr(dl.http, "download", _fail)

    result = dl.download("http://example.com/v", job)

    assert result.ok is False, "失败不能误报成功"
    assert not stale.exists(), "开跑前必须清掉上次残留，否则会被误判成本次产物"


def test_is_kuaishou():
    from xydl.downloader import is_kuaishou

    assert is_kuaishou("https://www.kuaishou.com/short-video/3xabc")
    assert is_kuaishou("https://v.kuaishou.com/abc123")
    assert is_kuaishou("https://www.kwai.com/video/522066")
    assert not is_kuaishou("https://www.douyin.com/video/123")
    assert not is_kuaishou("https://www.bilibili.com/video/BV123")


def test_kuaishou_photo_url_full_flow(monkeypatch):
    """mock 网络，验证短链展开 → photoId → GraphQL → photoUrl 的完整链路。"""
    import json as _json
    from xydl.downloader import kuaishou_photo_url

    # 1) 短链展开：项目 http.request 返回最终 URL
    class _Resp:
        url = "https://www.kuaishou.com/short-video/3xk8abc"

    monkeypatch.setattr("xydl.http.request", lambda *a, **k: _Resp())

    # 2) did cookie + GraphQL：mock 标准库 urllib opener
    class _Cookie:
        name = "did"
        value = "web_test"

    class _CJ:
        def __iter__(self):
            return iter([_Cookie()])

    class _Resp2:
        def read(self):
            return _json.dumps({
                "data": {"visionVideoDetail": {
                    "photo": {"photoUrl": "https://cdn/xxx.mp4", "caption": "标题"}
                }}
            }).encode()

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp2()

    monkeypatch.setattr("http.cookiejar.CookieJar", lambda: _CJ())
    monkeypatch.setattr("urllib.request.build_opener", lambda *a: _Opener())

    url, caption = kuaishou_photo_url("https://v.kuaishou.com/abc")
    assert url == "https://cdn/xxx.mp4"
    assert caption == "标题"


def test_kuaishou_photo_url_returns_empty_on_captcha(monkeypatch):
    """GraphQL 返回滑块拦截（无 photoUrl）时应返回空串，不抛异常。"""
    import json as _json
    from xydl.downloader import kuaishou_photo_url

    class _Resp:
        url = "https://www.kuaishou.com/short-video/3xk8abc"
    monkeypatch.setattr("xydl.http.request", lambda *a, **k: _Resp())

    class _Cookie:
        name = "did"
        value = "web_test"
    class _CJ:
        def __iter__(self):
            return iter([_Cookie()])
    class _Resp2:
        def read(self):
            return _json.dumps({"data": {"result": 400002}}).encode()
    class _Opener:
        def open(self, req, timeout=None):
            return _Resp2()
    monkeypatch.setattr("http.cookiejar.CookieJar", lambda: _CJ())
    monkeypatch.setattr("urllib.request.build_opener", lambda *a: _Opener())

    url, caption = kuaishou_photo_url("https://v.kuaishou.com/abc")
    assert url == ""
    assert caption == ""


def test_needs_cookie():
    from xydl.downloader import _needs_cookie

    assert _needs_cookie("该站点风控要求带 cookie")
    assert _needs_cookie("Fresh cookies are needed")
    assert not _needs_cookie("直链：这个链接返回的是网页")
    assert not _needs_cookie("")


def test_download_retries_with_browser_cookies(config: Config, tmp_path: Path,
                                               monkeypatch):
    """★ 回归：下载报「需要 cookie」且没配 cookie 时，静默读浏览器 cookie 重试。"""
    from xydl.downloader import DownloadResult

    job = tmp_path / "job"
    job.mkdir()
    dl = Downloader(config)

    calls: list[bool] = []

    def _dl(url, job_dir, *, title_hint="", progress=None, cancel=None):
        calls.append(1)
        # 第一次（没 cookie）失败，第二次（带 cookie）成功
        if len(calls) == 1:
            return DownloadResult(False, engine="ytdlp", error="该站点风控要求带 cookie")
        return DownloadResult(True, engine="ytdlp", file_path=str(job / "ok.mp4"),
                              title="ok", size_bytes=100)

    monkeypatch.setattr(dl.ytdlp, "download", _dl)
    monkeypatch.setattr(dl.ytdlp, "detect_browsers", lambda: ["edge"])
    # 测试 fixture 默认关掉 yt-dlp（走直链），这里强制开
    monkeypatch.setattr(dl, "prefer_ytdlp", True)
    monkeypatch.setattr(type(dl.ytdlp), "available", property(lambda self: True))
    monkeypatch.setattr(dl.http, "download",
                        lambda *a, **k: DownloadResult(False, engine="http", error="x"))

    result = dl.download("https://www.douyin.com/video/123", job)
    assert result.ok is True, "读浏览器 cookie 重试后应该成功"
    assert len(calls) == 2, "应该重试了一次"


def test_download_falls_back_to_cdp_for_douyin(config: Config, tmp_path: Path,
                                               monkeypatch):
    """★ 抖音：读浏览器 cookie 也失败时，降级到 CDP 拿明文 cookie 重试。"""
    from xydl.downloader import DownloadResult

    job = tmp_path / "job"
    job.mkdir()
    dl = Downloader(config)

    calls: list[int] = []

    def _dl(url, job_dir, *, title_hint="", progress=None, cancel=None):
        calls.append(1)
        if len(calls) == 1:
            return DownloadResult(False, engine="ytdlp", error="该站点风控要求带 cookie")
        return DownloadResult(True, engine="ytdlp", file_path=str(job / "ok.mp4"),
                              title="ok", size_bytes=100)

    monkeypatch.setattr(dl, "prefer_ytdlp", True)
    monkeypatch.setattr(type(dl.ytdlp), "available", property(lambda self: True))
    monkeypatch.setattr(dl.ytdlp, "download", _dl)
    # 浏览器里没有 cookie 可读
    monkeypatch.setattr(dl.ytdlp, "detect_browsers", lambda: [])
    # CDP 拿到明文 cookie
    monkeypatch.setattr("xydl.cdp_cookies.douyin_guest_cookies",
                        lambda url: {"s_v_web_id": "x", "ttwid": "y", "UIFID": "z"})
    monkeypatch.setattr(dl.http, "download",
                        lambda *a, **k: DownloadResult(False, engine="http", error="x"))

    result = dl.download("https://www.douyin.com/video/123", job)
    assert result.ok is True, "CDP 拿 cookie 重试后应该成功"
    assert len(calls) == 2, "应该先失败一次、CDP 重试一次"


def test_non_douyin_does_not_use_cdp(config: Config, tmp_path: Path, monkeypatch):
    """非抖音站点缺 cookie 时，不该去启动 Edge CDP。"""
    from xydl.downloader import DownloadResult

    job = tmp_path / "job"
    job.mkdir()
    dl = Downloader(config)

    monkeypatch.setattr(dl, "prefer_ytdlp", True)
    monkeypatch.setattr(type(dl.ytdlp), "available", property(lambda self: True))
    monkeypatch.setattr(dl.ytdlp, "download",
                        lambda *a, **k: DownloadResult(False, engine="ytdlp", error="该站点风控要求带 cookie"))
    monkeypatch.setattr(dl.ytdlp, "detect_browsers", lambda: [])
    called = []
    monkeypatch.setattr("xydl.cdp_cookies.douyin_guest_cookies", lambda url: called.append(1) or {})
    monkeypatch.setattr(dl.http, "download",
                        lambda *a, **k: DownloadResult(False, engine="http", error="x"))

    result = dl.download("https://www.xiaohongshu.com/explore/123", job)
    assert result.ok is False
    assert not called, "小红书不该触发抖音 CDP"


def test_douyin_falls_back_to_cdp_when_tikhub_fails(config: Config, tmp_path: Path,
                                                     monkeypatch):
    """★ 抖音：TikHub 失败时，降级到登录态 CDP 本地解析。"""
    from xydl.downloader import DownloadResult

    job = tmp_path / "job"
    job.mkdir()
    dl = Downloader(config)

    monkeypatch.setattr(dl, "prefer_ytdlp", True)
    monkeypatch.setattr(type(dl.ytdlp), "available", property(lambda self: True))
    # yt-dlp 失败
    monkeypatch.setattr(dl.ytdlp, "download",
                        lambda *a, **k: DownloadResult(False, engine="ytdlp", error="x"))
    # TikHub 失败（额度用完）
    monkeypatch.setattr("xydl.tikhub.resolve",
                        lambda url, key: ("", "额度用完"))
    # CDP 兜底成功
    monkeypatch.setattr("xydl.cdp_fetch.fetch_douyin_video",
                        lambda url, profile: ("https://cdn/dy.mp4", "测试视频"))
    monkeypatch.setattr("xydl.cdp_fetch.login_profile_dir",
                        lambda platform: tmp_path / "browser-login" / platform)
    (tmp_path / "browser-login" / "douyin").mkdir(parents=True)
    downloaded = {}
    monkeypatch.setattr(dl.http, "download",
                        lambda url, job_dir, **k: (
                            downloaded.update(url=url) or
                            DownloadResult(True, engine="http",
                                           file_path=str(job / "ok.mp4"),
                                           title=k.get("title_hint", ""), size_bytes=100)))

    result = dl.download("https://www.douyin.com/video/123", job)
    assert result.ok is True
    assert downloaded.get("url") == "https://cdn/dy.mp4"
