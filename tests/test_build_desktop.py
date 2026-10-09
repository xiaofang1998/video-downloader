"""打包脚本的 ffmpeg 处理逻辑。

这里最想防的回归是：**只拷 ffmpeg.exe 不拷它依赖的 DLL**。
conda 装的 ffmpeg 依赖同目录上百个 DLL，只拷 exe 的话用户双击直接闪退
（退出码 127，连一行报错都没有），而打包脚本自己会报「已带上 ffmpeg」——
看起来成功，实际是个废二进制。所以 ``_install_ffmpeg`` 拷完必须真跑一次。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_desktop as bd  # noqa: E402


def test_system_dlls_are_excluded():
    for name in ("KERNEL32.dll", "ntdll.dll", "api-ms-win-crt-heap-l1-1-0.dll",
                 "SHELL32.dll", "ext-ms-win-nothing-1-0"):
        assert bd._is_system_dll(name), f"{name} 应该被当成系统 DLL 跳过"


def test_normal_dlls_are_not_excluded():
    for name in ("avcodec-63.dll", "zlib.dll", "avformat-63.dll"):
        assert not bd._is_system_dll(name), f"{name} 是 ffmpeg 自带的，必须拷"


def test_pe_imports_reads_a_real_windows_binary():
    """拿解释器自己的 exe 验证解析器 —— 它一定是合法 PE。"""
    deps = bd._pe_imports(Path(sys.executable))
    assert deps, "解析不出来说明 PE 解析写错了"
    low = [d.lower() for d in deps]
    # CPython on Windows 必然链接这几个
    assert any("kernel32" in d for d in low)
    assert all(d.endswith((".dll", ".DLL")) for d in deps)


def test_pe_imports_rejects_non_pe(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"hello world" * 10)
    with pytest.raises(ValueError):
        bd._pe_imports(junk)


def test_collect_deps_takes_transitive_and_skips_system(tmp_path, monkeypatch):
    """依赖要顺着导入表往下追（传递闭包），但系统提供的不能带走。

    tool.exe → private.dll → deeper.dll，同时引用 KERNEL32 和一个源目录里
    根本没有的 ghost.dll（那种只能靠系统提供）。
    """
    src = tmp_path / "bin"
    src.mkdir()
    exe = src / "tool.exe"
    for n in ("tool.exe", "private.dll", "deeper.dll"):
        (src / n).write_bytes(b"MZ")

    fake_imports = {
        "tool.exe": ["private.dll", "KERNEL32.dll", "ghost.dll"],
        "private.dll": ["deeper.dll", "api-ms-win-crt-math-l1-1-0.dll"],
        "deeper.dll": [],
    }
    monkeypatch.setattr(bd, "_pe_imports", lambda p: fake_imports[p.name])

    got = [p.name for p in bd._collect_deps(exe)]
    assert sorted(got) == ["deeper.dll", "private.dll", "tool.exe"]
    assert not any(bd._is_system_dll(n) for n in got)
    assert "ghost.dll" not in got, "源目录里没有的依赖不该进拷贝清单"


def test_ffmpeg_works_reports_false_for_a_broken_binary(tmp_path):
    """放一个跑不起来的假 exe，_ffmpeg_works 必须说不行 —— 不能盲目报成功。"""
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"MZ" + b"\0" * 200)
    assert bd._ffmpeg_works(fake) is False


def test_candidate_list_is_deduplicated(monkeypatch, tmp_path):
    """同一路径被多个来源找到时只试一次，否则 131MB 要白拷好几遍。"""
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"MZ")

    monkeypatch.setattr(bd, "ROOT", tmp_path)
    (tmp_path / "ffmpeg").mkdir()
    (tmp_path / "ffmpeg" / "ffmpeg.exe").write_bytes(b"MZ")

    def _which(name):
        return str(fake) if name in ("ffmpeg.exe", "ffmpeg") else None

    monkeypatch.setattr(bd.shutil, "which", _which)

    cands = bd._ffmpeg_candidates()
    keys = [str(p).lower() for p in cands]
    assert len(keys) == len(set(keys)), f"候选列表有重复：{keys}"
    assert any(Path(k) == fake for k in keys)
