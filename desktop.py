#!/usr/bin/env python3
"""桌面应用入口：起本地服务 + 开一个像原生应用的窗口。

打包成 exe 之后用户双击的就是它；开发时也能直接跑：

    python desktop.py

**为什么不用 pywebview（踩过坑，别再改回去）**

pywebview 内嵌 WebView2 在初始化失败时（缺桌面会话、WebView2 异常等）会
**递归地把自己重新拉起来**：实测一次启动炸出 50+ 个进程，boot.log 里
每个新进程的 ppid 都指向第一个进程自己。对要卖出去的软件来说，
这个失败模式不可接受。

改用浏览器自带的「应用模式」：

* ``msedge --app=<url>`` 出来的窗口没有地址栏 / 标签页 / 书签栏，就是个独立窗口
* ``--user-data-dir`` 给这个应用单独一份配置，不与用户日常浏览器互相污染
* 这个浏览器进程是**我们自己的子进程**，用户关窗 → 进程退出 → 我们跟着收摊，
  生命周期是干净的

代价是内核仍是浏览器 —— 对下载工具来说可以忽略，换来的是「不挑环境、不会炸」。
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 允许直接 `python desktop.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))

from xydl.channels import build_channels                       # noqa: E402
from xydl.channels.base import ChannelManager                  # noqa: E402
from xydl.channels.console import ConsoleChannel, PortInUseError  # noqa: E402
from xydl.config import Config, app_dir, is_frozen             # noqa: E402
from xydl.pipeline import Pipeline                             # noqa: E402
from xydl.store import Store                                   # noqa: E402

APP_NAME = "视频下载工具"
WINDOW_TITLE = APP_NAME
WINDOW_W, WINDOW_H = 1180, 820

#: 开窗后等页面第一次连上来的上限（秒）
FIRST_SEEN_TIMEOUT = 60.0
#: 页面安静多久算窗口已关（页面每 2 秒轮询一次，留足余量）
IDLE_TIMEOUT = 15.0


def _setup_logging() -> None:
    """打包成窗口程序后没有控制台，把输出接到日志文件，出问题还能查。"""
    if not is_frozen():
        return
    try:
        log_dir = app_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(log_dir / "desktop.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = handle
        sys.stderr = handle
    except OSError:
        pass


def _alert(message: str) -> None:
    """窗口程序没有控制台，致命错误至少弹个框，别让用户双击之后什么都没有。"""
    print(message, file=sys.stderr)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, WINDOW_TITLE, 0x10)
    except Exception:  # noqa: BLE001 - 非 Windows / 没装 ctypes 就算了
        pass


def _trace(stage: str) -> None:
    """启动阶段追踪：记清每一步、进程号和父进程号，出问题不用靠猜。"""
    try:
        path = app_dir() / "logs" / "boot.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} "
                     f"ppid={os.getppid()} {stage}\n")
    except OSError:
        pass


def _looks_like_us(port: int) -> bool:
    """这个端口上跑的是不是我们自己（另一个实例）？"""
    from xydl import http as http_util

    try:
        resp = http_util.request("GET", f"http://127.0.0.1:{port}/api/state",
                                 timeout=2, proxy="")
    except Exception:  # noqa: BLE001
        return False
    if not resp.ok:
        return False
    try:
        return bool(resp.json().get("ok"))
    except Exception:  # noqa: BLE001
        return False


def _pick_console(config: Config, pipeline: Pipeline) -> tuple[ConsoleChannel, bool]:
    """装控制台渠道。返回 (渠道, 是不是接管了已存在的实例)。

    端口策略：
      1. 配的端口能起 → 正常起
      2. 起不来但那个端口上是**我们自己** → 说明已经开了一个窗口，
         直接复用它，不要再起第二个（否则两份程序抢同一个 SQLite）
      3. 起不来又是别人的 → 让系统随便给个空闲端口，保证一定能用
    """
    wanted = config.get("server.port", 8765)
    channel = ConsoleChannel(pipeline, config)
    try:
        channel.start()
        return channel, False
    except PortInUseError:
        pass

    if _looks_like_us(int(wanted)):
        print(f"[desktop] 检测到已经在运行（端口 {wanted}），直接复用那个实例")
        return channel, True

    print(f"[desktop] 端口 {wanted} 被别的程序占着，改用一个空闲端口")
    config.set("server.port", 0)
    channel = ConsoleChannel(pipeline, config)
    channel.start()
    return channel, False


def _find_browser() -> str:
    """找一个 Chromium 系浏览器用来开应用窗口。"""
    pf = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    pf86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    candidates = [
        pf86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        pf / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        pf / "Google" / "Chrome" / "Application" / "chrome.exe",
        pf86 / "Google" / "Chrome" / "Application" / "chrome.exe",
        local / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for exe in candidates:
        if exe.is_file():
            return str(exe)
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _keep_alive(url: str) -> None:
    """兜底之后要保持进程活着，否则服务一停页面就成空白。"""
    print(f"[desktop] 服务运行中：{url}")
    print("[desktop] 结束后台请关闭这个窗口 / 按 Ctrl+C")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def _profile_dir() -> Path:
    """浏览器 profile 放 ``%LOCALAPPDATA%``，不放 exe 同级。

    放同级的后果很具体：用户装完发现目录下莫名多出一百多 MB 的缓存，
    而且整个目录拷给别人时连缓存一起带走。放 LOCALAPPDATA 是 Windows
    应用的惯例位置，也不影响重装。建不出来就退回 exe 同级，不能因此开不了窗。
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    try:
        if base:
            p = Path(base) / APP_NAME / "browser-data"
            p.mkdir(parents=True, exist_ok=True)
            return p
    except OSError as exc:
        print(f"[desktop] 用不了 LOCALAPPDATA（{exc}），profile 退回程序目录")
    p = app_dir() / "browser-data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _open_window(url: str, console: ConsoleChannel) -> None:
    """开一个应用式窗口，并一直等到用户把它关掉。

    **窗口是否还开着，靠页面心跳判断，不看浏览器进程。**
    Edge 的启动器进程和真正的浏览器进程不是同一个，`proc.wait()` 常常立刻返回 ——
    照着它走会出现「窗口刚打开、服务就停了」，页面随即变成无法访问（这个坑真踩过）。
    页面本身每 2 秒轮询一次 ``/api/state``，所以：
    先等第一条请求出现（证明窗口确实起来了），之后只要 ``IDLE_TIMEOUT`` 秒没动静，
    就认为窗口关了，收摊退出。
    """
    browser = _find_browser()
    if not browser:
        _trace("no chromium browser -> default browser")
        print("[desktop] 没找到 Edge/Chrome，用默认浏览器打开（会带地址栏）")
        import webbrowser

        webbrowser.open(url)
        _keep_alive(url)
        return

    # 独立 user-data-dir 很关键：不共用用户日常浏览器的 profile，
    # 一是互不污染，二是不会因为「已有实例」被转交出去。
    profile = _profile_dir()
    args = [
        browser,
        f"--app={url}",
        f"--window-size={WINDOW_W},{WINDOW_H}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
    ]
    _trace(f"launching app window via {Path(browser).name}")
    try:
        # 不等这个进程 —— 见 docstring
        subprocess.Popen(args)
    except OSError as exc:
        _alert(f"打不开窗口：{exc}\n\n你可以手动在浏览器访问 {url}")
        _keep_alive(url)
        return

    print(f"[desktop] 窗口已打开（{Path(browser).name} 应用模式）")
    print("[desktop] 关掉窗口即退出程序")

    deadline = time.time() + FIRST_SEEN_TIMEOUT
    while time.time() < deadline:
        if console.last_request_at:
            break
        time.sleep(0.3)

    if not console.last_request_at:
        _trace("window never connected -> keep alive")
        print("[desktop] ⚠ 窗口没有连上来，服务继续在后台运行")
        _keep_alive(url)
        return

    _trace("window connected")
    while True:
        if time.time() - console.last_request_at > IDLE_TIMEOUT:
            _trace("window gone -> exit")
            print("[desktop] 窗口已关闭，退出")
            return
        time.sleep(1.0)


def main() -> int:
    _trace("main() 进入")
    config = Config.load().apply_env_overrides()
    dirs = config.ensure_dirs()
    store = Store(dirs["data_dir"] / "xydl.sqlite3")
    pipeline = Pipeline(config, store)

    print(f"[desktop] {WINDOW_TITLE}")
    print(f"[desktop] 数据目录 {app_dir()}")

    # 控制台由这里自己装配（要处理端口回退/复用），所以别让 build_channels 再来一遍
    config.set("channels.console.enabled", False)
    try:
        console, reused = _pick_console(config, pipeline)
    except Exception as exc:  # noqa: BLE001
        _alert(f"{WINDOW_TITLE} 启动失败：无法启动本地服务\n\n{exc}")
        store.close()
        return 2

    url = f"http://127.0.0.1:{console.actual_port}/"
    print(f"[desktop] 界面地址 {url}")

    if reused:
        # 已经有实例在跑了，只需要再开一个窗口指向它
        try:
            _open_window(url, console)
        finally:
            store.close()
        return 0

    # 其余渠道（收件箱 / 闲鱼桥接）按配置正常启用
    others = build_channels(pipeline, config)
    manager = ChannelManager(others)

    pipeline.start()
    manager.start()
    for name, exc in manager.errors:
        print(f"[desktop] 渠道 [{name}] 启动失败（不影响下载）：{exc}",
              file=sys.stderr)

    try:
        _open_window(url, console)
    finally:
        _trace("清理并退出")
        manager.stop()
        pipeline.stop()
        console.stop()
        store.close()
    return 0


if __name__ == "__main__":
    # PyInstaller 下的标准动作：没有它，任何用到多进程的第三方库
    # 都可能让打包出来的 exe 反复重新执行自己。
    multiprocessing.freeze_support()

    # ── 子命令分发（必须在 _setup_logging 之前，否则 yt-dlp 的 stdout 被吞）──
    argv = sys.argv[1:]
    if argv and argv[0] == "--run-yt-dlp":
        # 打包后 sys.executable 是应用自己，downloader 就是靠这个入口调 yt-dlp 的。
        from yt_dlp import main as ytdlp_main

        raise SystemExit(ytdlp_main(argv[1:]))
    if argv:
        # 没带这个专用入口、却带着参数被启动 = 有人（旧代码或某个第三方库）
        # 把我们的 exe 当成 Python 解释器在用。这一路下去就是「自己启动自己」的
        # 无限递归 —— 实测一次双击能炸出 50+ 进程。直接拒。
        print(f"[desktop] 收到不认识的参数 {argv}，拒绝启动"
              "（这是防止被当成 Python 解释器递归启动的护栏）", file=sys.stderr)
        raise SystemExit(2)

    _setup_logging()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as _exc:  # noqa: BLE001 - 双击启动，崩了必须让人看见
        import traceback

        _alert(
            f"{WINDOW_TITLE} 启动失败：\n\n{type(_exc).__name__}: {_exc}\n\n"
            f"详细信息见日志：{app_dir() / 'logs' / 'desktop.log'}"
        )
        traceback.print_exc()
        raise SystemExit(1)
