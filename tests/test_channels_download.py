"""视频号「下载 + 解密」集成测试。

之前只测了解密算法本身（``test_wxdecrypt.py``）和解析链（``test_wechat_channels.py``），
但**两者接起来**的那一段（``Downloader.download_channels_media`` /
``download_channels_url``）没有测试 —— 而恰恰是那一段决定了交付给客户的文件
能不能播。这里用本地 HTTP 服务器 + 真实密钥流构造一个「视频号加密 MP4」，
把整条链路真跑一遍。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xydl import wechat_channels as wc
from xydl import wxdecrypt
from xydl.config import Config
from xydl.downloader import DownloadResult, Downloader

#: 真实样本的 decode_key 与其解密后的 MP4 头（唯一真相源在 xydl/wxdecrypt.py）
KEY = wxdecrypt.SELFTEST_DECODE_KEY
PLAIN_HEAD = bytes.fromhex(wxdecrypt.SELFTEST_PLAINTEXT32_HEX)
#: 被加密的字节数
HEAD_BYTES = wxdecrypt.HEAD_BYTES
#: 真实视频号 CDN 直链的最后一段（是接口名，不是文件名）
CDN_NAME = "stodownload"


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config({
        "paths": {"data_dir": str(tmp_path / "data"),
                  "download_dir": str(tmp_path / "downloads")},
        "net": {"proxy": "none", "timeout_sec": 5},
        "download": {"prefer_ytdlp": False},
    })
    cfg.ensure_dirs()
    return cfg


def _make_encrypted_mp4(root: Path, name: str = CDN_NAME) -> Path:
    """造一个「视频号加密 MP4」：明文前缀 XOR 真实密钥流。

    明文形状照抄真实样本（``ftyp(32)`` + 声明长度超出前缀的 ``mdat``）——
    形状不对的话 box 链校验会把"正确解密"也判成非法，测试就失去意义了。

    默认文件名用 ``stodownload`` —— 真实视频号 CDN 直链的最后一段就是它
    （``finder.video.qq.com/251/20302/stodownload?...``），这是个**接口名**而不是
    文件名，引擎应当用标题提示顶掉它。
    """
    mdat_size = wxdecrypt.SELFTEST_FILE_SIZE - wxdecrypt.SELFTEST_MDAT_OFFSET
    plain = (PLAIN_HEAD + mdat_size.to_bytes(4, "big") + b"mdat"
             + bytes(HEAD_BYTES - 40))
    stream = wxdecrypt.generate_keystream(KEY, HEAD_BYTES)
    cipher = bytes(a ^ b for a, b in zip(plain, stream))
    target = root / name
    target.write_bytes(cipher)
    return target


def test_encrypted_sample_really_needs_decrypt(sample_server, tmp_path):
    """先确认「素材」确实是坏的 —— 否则下面的集成测试等于没测。"""
    served = _make_encrypted_mp4(tmp_path / "serve").read_bytes()
    assert not wxdecrypt.looks_like_media(served[:32])
    assert served[:32] != PLAIN_HEAD


def test_download_channels_media_decrypts_end_to_end(config, sample_server,
                                                     tmp_path):
    """核心用例：下载 → 发现是加密格式 → 解密 → 产物是合法 MP4。"""
    _make_encrypted_mp4(tmp_path / "serve")
    downloader = Downloader(config)
    job_dir = tmp_path / "job"

    media = wc.ChannelsMedia(ok=True, url=sample_server.url(CDN_NAME),
                             title="某某短剧-第1集", decode_key=KEY, via="tikhub")
    result = downloader.download_channels_media(media, job_dir)

    assert result.ok, result.error
    assert result.engine == "wechat_channels+http"
    assert result.meta["decrypted"] is True
    assert result.meta["channels_decode_key"] == KEY
    assert result.meta["channels_via"] == "tikhub"

    head = Path(result.file_path).read_bytes()[:32]
    assert head == PLAIN_HEAD, "交付出去的必须是解密后的可播放 MP4"
    assert wxdecrypt.looks_like_media(head)
    # 名字要来自标题，不能是 CDN 的接口名（stodownload.mp4 这种）
    assert Path(result.file_path).name == "某某短剧-第1集.mp4"


def test_download_channels_media_passes_headers(config, sample_server, tmp_path,
                                                monkeypatch):
    """抓包通道拿到的 Cookie/Referer 必须真的发给 CDN。"""
    _make_encrypted_mp4(tmp_path / "serve")
    downloader = Downloader(config)
    seen: list[dict | None] = []
    real = downloader.http.download

    def spy(url, job_dir, **kwargs):
        seen.append(kwargs.get("headers"))
        return real(url, job_dir, **kwargs)

    monkeypatch.setattr(downloader.http, "download", spy)
    media = wc.ChannelsMedia(ok=True, url=sample_server.url(CDN_NAME),
                             decode_key=KEY,
                             headers={"Referer": "https://channels.weixin.qq.com/",
                                      "Cookie": "wxuin=1"})
    result = downloader.download_channels_media(media, tmp_path / "job")
    assert result.ok, result.error
    assert seen == [media.headers]


def test_download_channels_media_retries_without_headers_on_403(
        config, sample_server, tmp_path, monkeypatch):
    """社区经验：视频号 CDN 有时因为带了 Referer 反而 403，去掉它再试一次。

    **只丢 Referer，Cookie 必须留着** —— Cookie 是登录态，丢了这条内容多半
    就再也下不了（它正是为了这种情况才从浏览器里捞出来的）。
    """
    _make_encrypted_mp4(tmp_path / "serve")
    downloader = Downloader(config)
    calls: list[dict | None] = []
    real = downloader.http.download

    def flaky(url, job_dir, **kwargs):
        calls.append(kwargs.get("headers"))
        if len(calls) == 1:
            return DownloadResult(False, engine="http",
                                  error="HTTPError: HTTP Error 403: Forbidden")
        return real(url, job_dir, **kwargs)

    monkeypatch.setattr(downloader.http, "download", flaky)
    media = wc.ChannelsMedia(ok=True, url=sample_server.url(CDN_NAME),
                             decode_key=KEY,
                             headers={"Referer": "https://channels.weixin.qq.com/",
                                      "Cookie": "wxuin=1; sessionid=abc"})
    result = downloader.download_channels_media(media, tmp_path / "job")

    assert result.ok, result.error
    assert len(calls) == 2, "第一次带 header 失败后必须去掉 Referer 重试"
    assert calls[0] is not None
    retry = calls[1]
    assert retry is not None, "重试必须仍然带 Cookie，不能把 headers 整个丢掉"
    assert "Referer" not in retry and "referer" not in retry
    assert retry.get("Cookie") == "wxuin=1; sessionid=abc"


def test_forbidden_detection_does_not_trigger_on_unrelated_numbers():
    """403 判定不能是裸的子串匹配（"超时 1403 秒"也会命中）。"""
    from xydl.downloader import _is_forbidden

    assert _is_forbidden("HTTPError: HTTP Error 403: Forbidden")
    assert _is_forbidden("403 Forbidden")
    assert not _is_forbidden("请求超时（1403s）")
    assert not _is_forbidden("")
    assert not _is_forbidden("HTTP Error 500")


def test_download_channels_media_keeps_plain_mp4_untouched(config, sample_server,
                                                           tmp_path):
    """没被加密的文件（decode_key 为空）不该被误改。"""
    root = tmp_path / "serve"
    (root / "plain.mp4").write_bytes(PLAIN_HEAD + bytes(4096))
    before = (root / "plain.mp4").read_bytes()

    downloader = Downloader(config)
    media = wc.ChannelsMedia(ok=True, url=sample_server.url("plain.mp4"),
                             decode_key="")
    result = downloader.download_channels_media(media, tmp_path / "job")

    assert result.ok, result.error
    assert Path(result.file_path).read_bytes() == before
    assert result.meta.get("decrypted") is not True


def test_download_channels_media_fails_loudly_when_undecryptable(
        config, sample_server, tmp_path):
    """加密但没 decode_key：必须失败，**绝不能**把花屏文件当成功交付。"""
    _make_encrypted_mp4(tmp_path / "serve")
    downloader = Downloader(config)
    media = wc.ChannelsMedia(ok=True, url=sample_server.url(CDN_NAME),
                             decode_key="")

    result = downloader.download_channels_media(media, tmp_path / "job")
    assert not result.ok
    assert "解密" in result.error


def test_download_channels_media_respects_auto_decrypt_off(config, sample_server,
                                                           tmp_path):
    config.set("download.wechat_channels.auto_decrypt", False)
    _make_encrypted_mp4(tmp_path / "serve")
    downloader = Downloader(config)
    media = wc.ChannelsMedia(ok=True, url=sample_server.url(CDN_NAME),
                             decode_key=KEY)

    result = downloader.download_channels_media(media, tmp_path / "job")
    assert not result.ok
    assert "auto_decrypt" in result.error


def test_download_channels_url_wires_resolve_media(config, sample_server,
                                                   tmp_path, monkeypatch):
    """门面：解析链给出的直链要真的被下载并解密。"""
    _make_encrypted_mp4(tmp_path / "serve")
    downloader = Downloader(config)

    def fake_resolve(url, cfg, **kwargs):
        return wc.ChannelsMedia(ok=True, url=sample_server.url(CDN_NAME),
                                title="出狱后，我举世无双-第1集", decode_key=KEY,
                                via="tikhub")

    monkeypatch.setattr(wc, "resolve_media", fake_resolve)
    result = downloader.download_channels_url(
        "https://weixin.qq.com/sph/AJizVCRhAt", tmp_path / "job")

    assert result.ok, result.error
    assert wxdecrypt.looks_like_media(Path(result.file_path).read_bytes()[:32])


def test_download_channels_url_reports_resolve_failure(config, tmp_path,
                                                       monkeypatch):
    """解析失败要原样把可执行的原因传上来，不能变成一句「下载失败」。"""
    downloader = Downloader(config)

    monkeypatch.setattr(wc, "resolve_media", lambda url, cfg, **kw: wc.ChannelsMedia(
        ok=False, error="TikHub：未配置 api_key｜建议：①填 delivery.tikhub.api_key"))
    result = downloader.download_channels_url(
        "https://weixin.qq.com/sph/AJizVCRhAt", tmp_path / "job")

    assert not result.ok
    assert "TikHub" in result.error
    assert "tikhub" in result.error


def test_download_facade_routes_channels_urls(config, sample_server, tmp_path,
                                              monkeypatch):
    """``Downloader.download`` 遇到视频号链接必须走专用通道，而不是丢给 yt-dlp。"""
    _make_encrypted_mp4(tmp_path / "serve")
    downloader = Downloader(config)
    used: list[str] = []

    def fake_ytdlp(*args, **kwargs):
        used.append("ytdlp")
        return DownloadResult(False, engine="ytdlp", error="不应走到这里")

    monkeypatch.setattr(wc, "resolve_media", lambda url, cfg, **kw: wc.ChannelsMedia(
        ok=True, url=sample_server.url(CDN_NAME), title="视频号视频", decode_key=KEY,
        via="tikhub"))
    monkeypatch.setattr(downloader.ytdlp, "download", fake_ytdlp)

    result = downloader.download("https://weixin.qq.com/sph/AJizVCRhAt",
                                 tmp_path / "job")
    assert result.ok, result.error
    assert used == [], "视频号不该尝试 yt-dlp（它没有视频号解析器，只会白跑一次）"


# ── 本地抓包工具给出来的"媒体直链"（wx_channels_download / mitmproxy 那种）────
CDN_URL = ("https://finder.video.qq.com/251/20302/stodownload"
           "?encfilekey=abc&token=xyz")


def test_is_channels_cdn_url():
    assert wc.is_channels_cdn_url(CDN_URL)
    assert wc.is_channels_cdn_url("https://wxapp.tc.qq.com/251/20304/stodownload")
    # 分享链接不是 CDN 直链
    assert not wc.is_channels_cdn_url("https://weixin.qq.com/sph/AJizVCRhAt")
    assert not wc.is_channels_cdn_url("https://www.bilibili.com/video/BV1x")


def test_cdn_direct_link_without_key_refuses_to_deliver_garbage(
        config, tmp_path, monkeypatch):
    """把加密的 CDN 直链丢进来时，**不能当成功**——否则交付出去是花屏文件。

    真需求：本地抓包工具（ltaoo/wx_channels_download、mitmproxy…）给的是这种直链，
    而它下下来的文件前 128KiB 是加密的。这里断言"拒收 + 给出可执行的下一步"。
    """
    plain = (PLAIN_HEAD + (wxdecrypt.SELFTEST_FILE_SIZE
                           - wxdecrypt.SELFTEST_MDAT_OFFSET).to_bytes(4, "big")
             + b"mdat" + bytes(HEAD_BYTES - 40))
    cipher = bytes(a ^ b for a, b in zip(
        plain, wxdecrypt.generate_keystream(KEY, HEAD_BYTES)))

    downloader = Downloader(config)

    def fake_http(url, job_dir, **kwargs):
        dest = Path(job_dir) / "stodownload.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(cipher)
        return DownloadResult(True, file_path=str(dest), engine="http",
                              size_bytes=len(cipher))

    monkeypatch.setattr(downloader.http, "download", fake_http)
    result = downloader.download(CDN_URL, tmp_path / "job")

    assert not result.ok, "加密文件绝不能当成功"
    assert "decode_key" in result.error
    assert "run.py decrypt" in result.error
    assert result.file_path, "文件要留着，方便用户拿到 key 后就地解密"


def test_cdn_direct_link_with_plain_file_passes(config, tmp_path, monkeypatch):
    """CDN 直链下到的**正常**文件（已经是不加密的）照常放行。"""
    downloader = Downloader(config)

    def fake_http(url, job_dir, **kwargs):
        dest = Path(job_dir) / "v.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(PLAIN_HEAD + bytes(4096))
        return DownloadResult(True, file_path=str(dest), engine="http")

    monkeypatch.setattr(downloader.http, "download", fake_http)
    result = downloader.download(CDN_URL, tmp_path / "job")
    assert result.ok, result.error


# ── `run.py decrypt`（本地抓下来的文件就地解密）────────────────────────
def test_decrypt_command_roundtrip(tmp_path, capsys):
    from types import SimpleNamespace

    import run as run_module

    _make_encrypted_mp4(tmp_path, name="enc.mp4")
    target = tmp_path / "enc.mp4"

    rc = run_module.cmd_decrypt(SimpleNamespace(path=str(target), decode_key=KEY))
    assert rc == 0, capsys.readouterr().out
    assert wxdecrypt.looks_like_media(target.read_bytes()[:32])

    # 再跑一次：已经是正常 MP4，应识别为"不需要解密"
    rc = run_module.cmd_decrypt(SimpleNamespace(path=str(target), decode_key=""))
    assert rc == 0
    assert "不需要解密" in capsys.readouterr().out


def test_decrypt_command_needs_key(tmp_path, capsys):
    from types import SimpleNamespace

    import run as run_module

    _make_encrypted_mp4(tmp_path, name="enc.mp4")
    rc = run_module.cmd_decrypt(SimpleNamespace(path=str(tmp_path / "enc.mp4"),
                                                decode_key=""))
    assert rc == 2
    out = capsys.readouterr().out
    assert "decode-key" in out


def test_decrypt_command_wrong_key_keeps_file(tmp_path, capsys):
    from types import SimpleNamespace

    import run as run_module

    _make_encrypted_mp4(tmp_path, name="enc.mp4")
    target = tmp_path / "enc.mp4"
    before = target.read_bytes()

    rc = run_module.cmd_decrypt(SimpleNamespace(path=str(target),
                                                decode_key="999999999"))
    assert rc == 1
    assert target.read_bytes() == before, "解不开就必须原样保留"
