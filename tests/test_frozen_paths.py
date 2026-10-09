"""打包（PyInstaller）后的路径行为测试。

这块逻辑一旦错了，表现是「开发能跑、发给用户就崩」——
下载的视频存进临时目录然后随进程消失。值得单独守住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from xydl import config as config_mod
from xydl.channels import console as console_mod
from xydl.config import Config


@pytest.fixture
def frozen(monkeypatch, tmp_path: Path):
    """把进程伪装成 PyInstaller 打出来的包。"""
    meipass = tmp_path / "_MEI12345"
    meipass.mkdir()
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "视频下载工具.exe"))
    return meipass, exe_dir


def test_dev_mode_uses_project_root():
    assert config_mod.is_frozen() is False
    assert config_mod.bundle_dir() == config_mod.ROOT
    assert config_mod.app_dir() == config_mod.ROOT


def test_frozen_bundle_dir_is_meipass(frozen):
    meipass, _ = frozen
    assert config_mod.is_frozen() is True
    assert config_mod.bundle_dir() == meipass


def test_frozen_app_dir_is_next_to_exe(frozen):
    """★ 可写数据必须落在 exe 旁边。

    放到 _MEIPASS 是灾难：onefile 模式下那是临时目录，进程一退就被删，
    用户下好的视频会凭空消失。
    """
    meipass, exe_dir = frozen
    assert config_mod.app_dir() == exe_dir
    assert config_mod.app_dir() != meipass


def test_frozen_relative_paths_resolve_next_to_exe(frozen):
    _, exe_dir = frozen
    cfg = Config({"paths": {"data_dir": "data", "download_dir": "downloads"}})
    assert cfg.path_of("data_dir") == exe_dir / "data"
    assert cfg.path_of("download_dir") == exe_dir / "downloads"


def test_frozen_absolute_paths_are_untouched(frozen, tmp_path):
    cfg = Config({"paths": {"download_dir": str(tmp_path / "自己定的")}})
    assert cfg.path_of("download_dir") == tmp_path / "自己定的"


def test_config_load_defaults_next_to_exe(frozen, monkeypatch):
    _, exe_dir = frozen
    # Config.load() 不传路径时应该写 exe 同级，而不是只读的包里
    cfg = Config.load()
    assert Path(str(cfg.path)).parent == exe_dir
    assert Path(str(cfg.path)).name == "config.json"


def test_index_file_resolves_in_dev():
    """开发时页面必须在真实路径上找得到。"""
    assert console_mod.INDEX_FILE.exists(), console_mod.INDEX_FILE
    assert console_mod.INDEX_FILE.name == "index.html"


def test_index_file_is_built_from_bundle_dir():
    """页面路径必须由 bundle_dir() 拼出来，不能写死 ROOT。

    打包后源码不在磁盘上，写死 ROOT 会 404 —— 窗口打开就是「控制台页面缺失」。
    """
    assert console_mod.INDEX_FILE == config_mod.bundle_dir() / "webui" / "index.html"


def test_ffmpeg_looks_next_to_exe(frozen):
    """随包发的 ffmpeg 必须被找到 —— 找不到的话 B站/YouTube 会下载失败。"""
    _, exe_dir = frozen
    bundled = exe_dir / "ffmpeg"
    bundled.mkdir()
    (bundled / "ffmpeg.exe").write_bytes(b"stub")

    cfg = Config({"download": {"ffmpeg_location": ""}})
    from xydl.downloader import Downloader

    candidates = Downloader(cfg).ytdlp._ffmpeg_candidates()
    assert str(bundled / "ffmpeg.exe") in candidates


def test_ytdlp_prefix_uses_python_in_dev():
    from xydl.downloader import _ytdlp_argv_prefix

    assert _ytdlp_argv_prefix()[1:] == ["-m", "yt_dlp"]


def test_ytdlp_prefix_never_reenters_the_app(frozen):
    """★★ 回归测试：打包后**绝不能**用 ``sys.executable + "-m yt_dlp"``。

    冻结后 ``sys.executable`` 是应用自己，那行命令等于「启动应用 -m yt_dlp」：
    main() 重入 → 又去探测 yt-dlp → 又启动自己……实测一次双击炸出 50+ 个进程，
    而且报错信息完全指不到这里。这是本项目最隐蔽的一个坑。
    """
    from xydl.downloader import _ytdlp_argv_prefix

    prefix = _ytdlp_argv_prefix()
    assert prefix[1] == "--run-yt-dlp"
    assert "-m" not in prefix and "yt_dlp" not in prefix


def test_browser_profile_does_not_pollute_the_install_dir(frozen, monkeypatch, tmp_path):
    """浏览器 profile 必须落在 %LOCALAPPDATA%，不能建在 exe 同级。

    放同级的实际后果：用户目录下莫名多出一百多 MB 缓存，而且整个安装目录
    拷给别人时连缓存一起带走。
    """
    _, exe_dir = frozen
    local = tmp_path / "localappdata"
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    import desktop

    profile = desktop._profile_dir()
    assert profile == local / "视频下载工具" / "browser-data"
    assert exe_dir not in profile.parents, "profile 不该出现在安装目录里"
    assert profile.is_dir()


def test_browser_profile_falls_back_when_localappdata_missing(frozen, monkeypatch):
    """环境里没有 LOCALAPPDATA 时也不能开不了窗 —— 退回程序目录。"""
    _, exe_dir = frozen
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    import desktop

    profile = desktop._profile_dir()
    assert profile == exe_dir / "browser-data"
    assert profile.is_dir()
