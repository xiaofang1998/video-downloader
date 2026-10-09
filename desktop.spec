# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onefile 单文件版）。

请用 `python build_desktop.py` 驱动，不要直接 `pyinstaller desktop.spec`
（脚本会先把 ffmpeg 连同依赖 DLL 收集到 build/_ffmpeg_stage/，再跑打包）。

产物是**单个** `dist/视频下载工具.exe`，双击即用，不依赖旁边任何目录。
代价：onefile 每次启动都要把内部约 170MB 解到临时目录，冷启动会慢几秒。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)  # SPECPATH 由 PyInstaller 注入（spec 所在目录）

# yt-dlp 的解析器是动态导入的（几百个 extractor 子模块），
# 必须 collect_all，否则打出来的包能启动但一下载就报「没有可用的解析器」。
ytdlp_datas, ytdlp_binaries, ytdlp_hidden = collect_all("yt_dlp")

hiddenimports = [
    "certifi",
    *ytdlp_hidden,
]

# 这些库一个都用不到，但很容易被间接拖进来，白白把包撑大几百 MB
excludes = [
    "tkinter", "matplotlib", "numpy", "pandas", "scipy",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "jupyter", "notebook", "pytest",
    # 桌面外壳走的是浏览器应用模式，不需要内嵌 WebView ——
    # pywebview 失败时会递归重开自己（一次炸出 50+ 进程），已经弃用。
    "webview", "clr_loader", "pythonnet",
]

# ── ffmpeg：build_desktop.py 已把 exe + 依赖 DLL 收集到这里 ─────────
# 放进 datas（前缀 ffmpeg），onefile 启动时会解到 _MEIPASS/ffmpeg/，
# ffmpeg.exe 从自身所在目录找 DLL，能正常跑。
ffmpeg_stage = ROOT / "build" / "_ffmpeg_stage" / "ffmpeg"
ffmpeg_datas = (
    [(str(p), "ffmpeg") for p in ffmpeg_stage.iterdir() if p.is_file()]
    if ffmpeg_stage.is_dir()
    else []
)

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=[*ytdlp_binaries],
    datas=[
        # 前端页面是随包资源，运行时从 _MEIPASS/webui/ 取
        (str(ROOT / "webui" / "index.html"), "webui"),
        *ytdlp_datas,
        *ffmpeg_datas,
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

# onefile：把 binaries + datas 一起打进 exe，不再 COLLECT 成目录。
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="视频下载工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 关键：不要控制台黑框，它是个桌面应用
    console=False,
    disable_windowed_traceback=False,
)
