#!/usr/bin/env python3
"""把项目打成 Windows 桌面应用 —— **单文件（onefile）**。

    python build_desktop.py

产物：``dist/视频下载工具.exe``（就一个文件，双击即用，不依赖旁边任何目录）。

为什么 onefile：用户要求「单独 exe 就能执行完整功能」，方便拷贝/分发。
代价：onefile 每次启动都要把内部约 170MB（含 ffmpeg）解到临时目录，
冷启动会慢几秒 —— 这是「一个文件搞定」换来的，属于可接受的权衡。
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "视频下载工具"
EXE_OUT = ROOT / "dist" / f"{APP_NAME}.exe"
FFMPEG_STAGE = ROOT / "build" / "_ffmpeg_stage" / "ffmpeg"

# ── Windows 自己一定有的 DLL ────────────────────────────────────────
# 这些不需要（也不应该）跟着打包：它们由系统提供，拷过去反而可能在别的
# 机器上因为版本不同出问题。api-ms-win-* 是 API Set，永远能解析。
_SYSTEM_DLLS = {
    "kernel32.dll", "ntdll.dll", "psapi.dll", "shell32.dll", "user32.dll",
    "advapi32.dll", "gdi32.dll", "ws2_32.dll", "wsock32.dll", "ole32.dll",
    "oleaut32.dll", "shlwapi.dll", "bcrypt.dll", "crypt32.dll", "msvcrt.dll",
    "ucrtbase.dll", "sechost.dll", "cfgmgr32.dll", "powrprof.dll",
    "setupapi.dll", "winmm.dll", "version.dll", "imm32.dll", "comdlg32.dll",
    "netapi32.dll", "userenv.dll", "propsys.dll", "windowscodecs.dll",
    "msimg32.dll", "rpcrt4.dll", "usp10.dll", "iphlpapi.dll", "dnsapi.dll",
    "dwrite.dll", "bcryptprimitives.dll", "avicap32.dll", "mfplat.dll",
    "mf.dll", "oleacc.dll", "uxtheme.dll", "dwmapi.dll", "sspicli.dll",
}


def _is_system_dll(name: str) -> bool:
    low = name.lower()
    return low in _SYSTEM_DLLS or low.startswith(("api-ms-win-", "ext-ms-win-"))


def _run(cmd: list) -> None:
    print("  $", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True)


# ── PE 导入表解析（纯标准库）────────────────────────────────────────
# 为什么自己写：Windows 上没装 pefile，也不想为一个打包脚本加第三方依赖。
# 只用到 struct，读的是 PE 规范里最稳的那几段。
def _pe_imports(path: Path) -> list[str]:
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise ValueError("不是 PE 文件")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew : e_lfanew + 4] != b"PE\0\0":
        raise ValueError("PE 签名不对")

    coff = e_lfanew + 4
    nsec, = struct.unpack_from("<H", data, coff + 2)
    size_opt, = struct.unpack_from("<H", data, coff + 16)
    opt = coff + 20
    magic, = struct.unpack_from("<H", data, opt)
    # 数据目录在可选头中的偏移：PE32 = 96，PE32+ = 112
    data_dirs = opt + (112 if magic == 0x20B else 96)

    sections = []
    sec_tab = opt + size_opt
    for i in range(nsec):
        _n, vsize, vaddr, rawsize, rawptr = struct.unpack_from(
            "<8sIIII", data, sec_tab + i * 40)
        sections.append((vaddr, vsize, rawptr, rawsize))

    def rva_to_off(rva: int) -> int | None:
        for vaddr, vsize, rawptr, rawsize in sections:
            if vaddr <= rva < vaddr + max(vsize, rawsize):
                return rawptr + (rva - vaddr)
        return None

    imp_rva, _imp_size = struct.unpack_from("<II", data, data_dirs + 8)
    if not imp_rva:
        return []

    out = []
    cur = imp_rva
    while True:
        off = rva_to_off(cur)
        if off is None:
            break
        _oft, _ts, _fc, name_rva, _ft = struct.unpack_from("<IIIII", data, off)
        if not name_rva and not _oft:
            break
        name_off = rva_to_off(name_rva)
        if name_off is None:
            break
        out.append(data[name_off:data.index(b"\0", name_off)].decode("ascii", "replace"))
        cur += 20
    return out


def _ffmpeg_candidates() -> list[Path]:
    """按优先级列出候选 ffmpeg.exe。

    优先级：项目自带 ffmpeg/ → PATH → 项目自己的发现逻辑（会扫常见安装位）。
    """
    found: list[Path] = []
    for p in (
        ROOT / "ffmpeg" / "ffmpeg.exe",
        ROOT / "ffmpeg" / "ffmpeg",
    ):
        if p.exists():
            found.append(p)

    for name in ("ffmpeg.exe", "ffmpeg"):
        which = shutil.which(name)
        if which:
            found.append(Path(which))

    try:
        sys.path.insert(0, str(ROOT))
        from xydl.config import Config
        from xydl.downloader import Downloader

        p = Downloader(Config.load()).ytdlp.find_ffmpeg()
        if p:
            found.append(Path(p))
    except Exception as exc:  # noqa: BLE001
        print(f"  （用项目逻辑找 ffmpeg 时出错：{exc}）")

    # 去重，保序
    seen: set[str] = set()
    uniq = []
    for p in found:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _collect_deps(exe: Path) -> list[Path]:
    """顺着导入表把 exe 传递依赖的 DLL 全找出来（只在源目录里找）。

    静态编译的 ffmpeg 会返回空列表 —— 那就只拷 exe 本身，最理想。
    """
    src_dir = exe.parent
    wanted: dict[str, Path] = {}
    queue = [exe.name]
    while queue:
        name = queue.pop()
        if name.lower() in wanted or _is_system_dll(name):
            continue
        path = src_dir / name
        if not path.exists():
            # 源目录里也没有，说明是系统提供的
            continue
        wanted[name.lower()] = path
        try:
            queue.extend(_pe_imports(path))
        except Exception as exc:  # noqa: BLE001
            print(f"    解析 {name} 的导入表失败：{exc}")
    return [wanted[k] for k in sorted(wanted)]


def _ffmpeg_works(exe: Path) -> bool:
    """拷完必须真跑一次 —— 不验证等于没拷。

    血泪教训：conda 装的 ffmpeg 依赖同目录上百个 DLL，只拷 exe 的话
    双击直接闪退（退出码 127，连报错都没有）。
    """
    try:
        r = subprocess.run([str(exe), "-version"],
                           capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0


def _stage_ffmpeg() -> None:
    """打包前把 ffmpeg（连带依赖 DLL）收集到 build/_ffmpeg_stage/ffmpeg/。

    onefile 下没法像 onedir 那样「打包完再往目录里塞」，必须在 PyInstaller
    分析阶段就把这些文件作为 datas 加进去，所以这里只是备好 staging，
    真正收进 exe 的是 desktop.spec 里的 ``ffmpeg_datas``。
    """
    print("\n查找 ffmpeg…")
    target_dir = FFMPEG_STAGE

    for cand in _ffmpeg_candidates():
        print(f"  候选 {cand}")
        try:
            deps = _collect_deps(cand)
        except Exception as exc:  # noqa: BLE001
            print(f"    ⚠ 解析依赖失败，跳过：{exc}")
            continue

        shutil.rmtree(target_dir, ignore_errors=True)
        target_dir.mkdir(parents=True, exist_ok=True)
        for src in deps:
            shutil.copy2(src, target_dir / src.name)

        exe = target_dir / cand.name
        kind = "静态" if len(deps) == 1 else f"带 {len(deps) - 1} 个 DLL"
        if _ffmpeg_works(exe):
            mb = sum(f.stat().st_size for f in target_dir.iterdir()) / 1024 / 1024
            print(f"  ✓ 已备好 ffmpeg（{kind}，{mb:.0f} MB），实测可用")
        else:
            shutil.rmtree(target_dir, ignore_errors=True)
            print(f"    ⚠ 这个拷过去跑不起来（依赖没拷全？），换下一个候选")
            continue

        # ffprobe 有就一起带，带不了也不影响主流程
        probe = cand.with_name("ffprobe" + cand.suffix)
        if probe.exists():
            try:
                for src in _collect_deps(probe):
                    shutil.copy2(src, target_dir / src.name)
                print("  ✓ 已带上 ffprobe")
            except Exception as exc:  # noqa: BLE001
                print(f"  （ffprobe 没带上：{exc}）")
        return

    print("  ⚠ 本机没找到能用的 ffmpeg，包里将不含它。")
    print("    B站/YouTube/小红书 这类站点的视频会下载失败（只有画面没声音）。")
    print("    修法：去 https://www.gyan.dev/ffmpeg/builds/ 下")
    print("    ffmpeg-release-essentials.zip，把里面的 ffmpeg.exe 放到项目根目录的")
    print("    ffmpeg/ 下，重新打包。")


def _clear_old_output() -> None:
    """把上一次的产物挪走，给这次打包腾位置。

    为什么不直接删：PyInstaller 会先把同名输出整个删掉，而上千个小文件的
    批量删除会被安全策略（沙箱 / 杀软）拦下 —— 表现是
    ``OSError: SHFileOperationW 失败: 0x2``，而且报错完全看不出是权限问题。
    **改名挪开是等价效果，且不受拦截** —— 代价只是旧产物堆在 dist/_old/ 里，
    需要你偶尔手动清一下。

    onefile 的产物是 ``dist/视频下载工具.exe``（文件），但老版本可能留下过
    onedir 的 ``dist/视频下载工具/``（目录），所以两个都处理。
    """
    graveyard = ROOT / "dist" / "_old"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in (EXE_OUT, ROOT / "dist" / APP_NAME, ROOT / "build"):
        if not path.exists():
            continue
        graveyard.mkdir(parents=True, exist_ok=True)
        target = graveyard / f"{path.name}-{stamp}"
        try:
            path.rename(target)
            print(f"旧产物已挪到   dist/_old/{target.name}（可以随时自己删）")
        except OSError as exc:
            print(f"⚠ 挪不动 {path}（{exc}），这次打包可能会失败")


def main() -> int:
    print(f"项目目录   {ROOT}")

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("\n❌ 没装 PyInstaller。先执行：\n   pip install pyinstaller")
        return 2

    _clear_old_output()

    # 打包前先把 ffmpeg 备好（staging 在 build/ 里，spec 会读它）
    _stage_ffmpeg()

    print("\n开始打包（第一次比较慢，几分钟）…\n")
    try:
        # 不加 --clean：那会让 PyInstaller 去删 build/，同样会被安全策略拦下
        _run([sys.executable, "-m", "PyInstaller", "--noconfirm",
              str(ROOT / "desktop.spec")])
    except subprocess.CalledProcessError as exc:
        print(f"\n❌ 打包失败（退出码 {exc.returncode}）")
        return 1

    if not EXE_OUT.exists():
        print(f"\n❌ 没找到产物：{EXE_OUT}")
        return 1

    size_mb = EXE_OUT.stat().st_size / 1024 / 1024
    print(f"\n✅ 打包完成")
    print(f"   产物   {EXE_OUT}")
    print(f"   体积   {size_mb:.0f} MB")
    print(f"\n   单文件，直接双击运行。")
    print(f"   数据（data/ downloads/ logs/ config.json）第一次运行会在 exe 同级生成。")
    print(f"   提示：onefile 冷启动要把内部解到临时目录，会比 onedir 慢几秒。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
