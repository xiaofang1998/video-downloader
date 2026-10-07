"""下载引擎：yt-dlp 封装 + 直链 HTTP 下载。

两个刻意的设计决定：

1. **每个订单一个独立子目录**（``downloads/<order_id>/``）。
   因为多个 worker 并发下载时，靠「扫描目录里最新文件」来定位产物会串单。
   隔离到独立目录后，结束后扫自己的目录就行，永远不会张冠李戴。

2. **先探测元数据，再下载**（两次调用 yt-dlp）。
   第一次 ``-J`` 拿到标题/时长/大小，用来命名文件和给买家报体积；
   第二次才真正下载并解析进度。多花一次请求，换来干净的文件名和可读的报错。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import linkparse
from .config import Config
from .http import HttpError, open_stream, parse_proxy_spec, system_proxy_url
from .models import DownloadResult, human_size
from .utils import clean_media_title, ensure_unique, sanitize_filename

#: 视为残留的中间文件
TEMP_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp", ".frag")
#: yt-dlp 的 DASH 分片命名：``<标题>.f<format_id>.<ext>``。
#: 这种文件**绝不是成品** —— 真实事故：没合流成功时它被当成下载结果交付给客户。
_FRAGMENT_RE = re.compile(r"\.f[A-Za-z0-9_]+\.[A-Za-z0-9]+$")
#: 子进程输出里最多保留多少行用于报错
MAX_LOG_LINES = 40


@dataclass
class Progress:
    """进度回调携带的信息。"""

    percent: float = 0.0
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0        # 字节/秒
    eta: str = ""
    stage: str = ""

    @property
    def speed_human(self) -> str:
        return f"{human_size(self.speed)}/s" if self.speed else ""


ProgressCallback = Callable[[Progress], None]


def _noop(_progress: Progress) -> None:
    return None


# ══════════════════════════════════════════════════════════════════════
#  yt-dlp
# ══════════════════════════════════════════════════════════════════════
_YTDLP_PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<pct>[\d.]+)%\s+of\s+~?\s*(?P<size>[\d.]+)(?P<sunit>\w+)"
    r"(?:\s+at\s+(?P<speed>[\d.]+|Unknown\s*B/s)(?P<spunit>\w+)?/s)?"
    r"(?:\s+ETA\s+(?P<eta>[\d:]+|Unknown))?",
    re.IGNORECASE,
)
_FINAL_PATH_PATTERNS = (
    re.compile(r'\[Merger\]\s+Merging formats into\s+"(.+)"'),
    re.compile(r"\[download\]\s+(.+?) has already been downloaded"),
    re.compile(r"\[download\]\s+Destination:\s*(.+)"),
    re.compile(r'^\[ExtractAudio\]\s+Destination:\s*(.+)$', re.MULTILINE),
)
_UNIT_MULT = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}
#: 常见的「这条链接本身就不该走通用下载器」错误，给出更人话的提示
_ERROR_HINTS = (
    (re.compile(r"Unsupported URL", re.I), "yt-dlp 不认识这个站点的链接"),
    (re.compile(r"Unable to download webpage|HTTP Error 4\d\d", re.I),
     "目标站点拒绝了访问（可能被反爬拦截，或在境外需要配代理）"),
    (re.compile(r"Failed to decrypt|app.?bound encryption|DPAPI", re.I),
     "浏览器 cookie 无法解密。Chrome/Edge 127 之后启用了 App-Bound 加密，"
     "yt-dlp 读不出来（这是已知限制）。改用 cookies.txt：在浏览器里装一个能导出 "
     "Netscape 格式的 cookie 扩展（如 Get cookies.txt LOCALLY），导出后把路径填到 "
     "config.json 的 download.cookies_file"),
    (re.compile(r"Could not copy (Chrome|Firefox) cookie database", re.I),
     "浏览器正在运行，cookie 数据库被锁住了。**把所有浏览器窗口完全关掉**"
     "（包括托盘/后台的 msedge.exe）再重试；或者改用 download.cookies_file "
     "指定一个导出的 cookies.txt"),
    (re.compile(r"The page needs to be reloaded|Sign in to confirm you.?re not a bot", re.I),
     "YouTube 判定这次请求可疑。**不要给它带 cookie** —— 别的浏览器会话的 "
     "cookie 反而会触发风控。把 download.cookie_mode 保持 auto（YouTube 不在 "
     "cookie_domains 里），或把 youtube.com 从 cookie_domains 里去掉"),
    (re.compile(r"Cookies? .{0,40}are needed", re.I),
     "该站点要求携带浏览器 cookie（抖音/西瓜/微博这类站点的常规风控，不是登录问题）。"
     "跑 `run.py login <站点>` 配一下，或改用 download.cookies_file。"
     "注意 cookie 是**站点特定**的：别把它发给 YouTube/TikTok，那些站点带了"
     "别的会话的 cookie 反而会被风控拒绝"),
    (re.compile(r"Private video|login required|Sign in to confirm|需要登录|"
                r"Login required|account.*required", re.I),
     "这个内容需要登录才能访问。配置 download.cookies_from_browser（如 \"edge\"）"
     "或 download.cookies_file 即可"),
    (re.compile(r"ffmpeg (is )?not installed|ffmpeg not found", re.I),
     "缺少 ffmpeg，无法合并高清音视频轨（B站/YouTube 等只有 DASH 分离流，必须装 ffmpeg）"),
    (re.compile(r"Requested format is not available", re.I),
     "没有匹配到可用画质。这类站点通常只提供 DASH 分离流（视频轨+音频轨分开），"
     "装上 ffmpeg 后即可自动合流下载"),
    (re.compile(r"No video formats found|no formats found", re.I),
     "这个页面里没有可下载的视频流（可能是纯图片/纯音频/需要登录）"),
    (re.compile(r"Video unavailable|This video is not available|已删除|不存在", re.I),
     "视频不存在或已被删除"),
    (re.compile(r"timed out|timeout", re.I), "下载超时（视频可能过大或网络太慢）"),
)


def _to_bytes(value: str, unit: str) -> int:
    try:
        return int(float(value) * _UNIT_MULT.get((unit or "B").upper(), 1))
    except (TypeError, ValueError):
        return 0


#: 只有这些错误值得换一套格式选择器重试；网络/登录类错误换格式也是白换
_FORMAT_RETRYABLE = re.compile(
    r"Requested format is not available"
    r"|No video formats found"
    r"|format is not available"
    r"|ffmpeg is not installed",
    re.IGNORECASE,
)


class YtDlpEngine:
    """封装 ``yt-dlp`` 命令行。

    调用方式优先用 ``<当前解释器> -m yt_dlp``，这样能保证用的是项目 venv 里
    装的那份，不会和系统里另一个版本打架；找不到再退回 PATH 上的 ``yt-dlp``。
    """

    name = "ytdlp"
    label = "yt-dlp"

    def __init__(self, config: Config):
        self.config = config
        self.timeout = float(config.get("download.task_timeout_sec", 1800))
        # yt-dlp 不读 Windows 注册表里的系统代理，必须把地址显式喂给它；
        # 所以 "auto" 要在这里就解析成具体地址。
        spec = parse_proxy_spec(config.get("download.proxy") or config.get("net.proxy"))
        self.proxy = system_proxy_url() if spec is None else spec
        self.cookies_from_browser = str(config.get("download.cookies_from_browser", "")).strip()
        self.cookies_file = str(config.get("download.cookies_file", "") or "").strip()
        self.quality = str(config.get("download.quality", "best")).strip().lower()
        self.max_filesize_mb = int(config.get("download.max_filesize_mb", 0) or 0)
        self.extra_args = [str(a) for a in (config.get("download.ytdlp_extra_args", []) or [])]
        self._base_cmd: list[str] | None = None
        #: ffmpeg 候选路径 → 实测能否运行（很贵，必须缓存）
        self._ffmpeg_ok: dict[str, bool] = {}
        self._ffmpeg_resolved: str | None = None

    # ── 可用性 ─────────────────────────────────────────────────────
    def _resolve_base_cmd(self) -> list[str] | None:
        if self._base_cmd is not None:
            return self._base_cmd or None
        # 1) 当前解释器里能不能 import yt_dlp
        try:
            probe = subprocess.run(
                [sys.executable, "-c", "import yt_dlp; print(yt_dlp.version.__version__)"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, env=self._child_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if probe.returncode == 0:
                self._base_cmd = [sys.executable, "-m", "yt_dlp"]
                self.version = (probe.stdout or "").strip()
                return self._base_cmd
        except (OSError, subprocess.SubprocessError):
            pass
        # 2) 退回 PATH 上的 yt-dlp
        exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
        if exe:
            self._base_cmd = [exe]
            self.version = ""
            return self._base_cmd
        self._base_cmd = []
        self.version = ""
        return None

    version: str = ""

    @property
    def available(self) -> bool:
        return self._resolve_base_cmd() is not None

    # ── ffmpeg 发现 ────────────────────────────────────────────────
    def probe_ffmpeg(self, path: str) -> bool:
        """实测这个 ffmpeg 能不能跑。

        必须实测、不能只看文件存在 —— 这个踩过坑：mise 的 shim ``ffmpeg.EXE``
        确实存在，但直接执行返回 0xC0000135（DLL 缺失）。把这种路径交给 yt-dlp
        的后果非常隐蔽：yt-dlp 不报错、退出码 0、却也**不做合流**，最后只留下
        视频轨和音频轨两个分片，看起来像"下载成功"。
        """
        if not path:
            return False
        # 路径里带 NUL 会让 CreateProcess 抛 ValueError，先挡住
        if "\x00" in path:
            return False
        cached = self._ffmpeg_ok.get(path)
        if cached is not None:
            return cached
        ok = False
        try:
            proc = subprocess.run(
                [path, "-version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                env=self._child_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            blob = ((proc.stdout or "") + (proc.stderr or "")).lower()
            ok = proc.returncode == 0 and "ffmpeg version" in blob
        except (OSError, subprocess.SubprocessError):
            ok = False
        self._ffmpeg_ok[path] = ok
        return ok

    @staticmethod
    def _ffmpeg_search_dirs() -> list[Path]:
        """托管工具的常见落点。

        之所以要枚举而不是只靠 PATH：用户从自己的终端跑 run.py 时，Cherry / scoop
        的 shims 目录往往不在 PATH 里；而且 mise 的 shims 在有依赖 DLL 的包
        （比如 conda 版 ffmpeg）上还会直接失效，必须落到真正的 Library/bin。
        """
        home = Path.home()
        appdata_env = os.environ.get("APPDATA", "")
        # APPDATA 不是所有环境都有（某些沙箱/服务进程会缺），用 Path.home() 兜底
        appdata = Path(appdata_env) if appdata_env else home / "AppData" / "Roaming"
        localappdata_env = os.environ.get("LOCALAPPDATA", "")
        localappdata = (Path(localappdata_env) if localappdata_env
                        else home / "AppData" / "Local")
        programdata = os.environ.get("ProgramData", "")

        dirs = [
            home / ".cherrystudio" / "bin",
            home / ".local" / "share" / "mise" / "shims",
            localappdata / "mise" / "shims",
            home / "scoop" / "shims",
            localappdata / "Microsoft" / "WinGet" / "Links",
            appdata / "CherryStudio" / "Toolchain" / "mise" / "shims",
        ]
        if programdata:
            dirs.append(Path(programdata) / "chocolatey" / "bin")

        # conda 版 ffmpeg 的真实二进制在 installs/<pkg>/<ver>/Library/bin
        installs = appdata / "CherryStudio" / "Toolchain" / "mise" / "installs"
        try:
            for pkg in installs.glob("*ffmpeg*"):
                for ver in pkg.iterdir():
                    if ver.is_dir():
                        dirs.append(ver / "Library" / "bin")
                        dirs.append(ver / "bin")
                        dirs.append(ver / ".mise-bins")
        except OSError:
            pass

        toolchain = appdata / "CherryStudio" / "Toolchain"
        try:
            dirs.extend(toolchain.glob("*/shims"))
        except OSError:
            pass
        return [d for d in dirs if str(d)]

    def _ffmpeg_candidates(self) -> list[str]:
        """按优先级给出候选路径（已去重，保持顺序）。"""
        out: list[str] = []

        def add(value: Path | str) -> None:
            text = str(value)
            if text and text not in out:
                out.append(text)

        configured = str(self.config.get("download.ffmpeg_location", "") or "").strip()
        if configured:
            path = Path(configured)
            if path.is_dir():
                for name in ("ffmpeg.exe", "ffmpeg"):
                    add(path / name)
            else:
                add(path)

        for name in ("ffmpeg.exe", "ffmpeg"):
            found = shutil.which(name)
            if found:
                add(found)

        for base in self._ffmpeg_search_dirs():
            for name in ("ffmpeg.exe", "ffmpeg"):
                add(base / name)
        return out

    def find_ffmpeg(self) -> str:
        """返回**实测可运行**的 ffmpeg 路径；找不到返回空串。结果会缓存。"""
        if self._ffmpeg_resolved is not None:
            return self._ffmpeg_resolved
        for candidate in self._ffmpeg_candidates():
            if self.probe_ffmpeg(candidate):
                self._ffmpeg_resolved = candidate
                return candidate
        self._ffmpeg_resolved = ""
        return ""

    def describe_ffmpeg(self) -> str:
        """给 doctor 用：说清楚找了哪些地方、为什么没成。"""
        working = self.find_ffmpeg()
        if working:
            return f"已启用 {working}"
        tried = self._ffmpeg_candidates()
        if not tried:
            return "未找到任何 ffmpeg 候选"
        broken = [p for p in tried if not self.probe_ffmpeg(p)]
        detail = f"找到 {len(tried)} 个候选但都不能运行" if broken else "没有候选"
        return f"{detail}（例如 {broken[0]}）" if broken else detail

    def has_ffmpeg(self) -> bool:
        return bool(self.find_ffmpeg())

    # ── 命令行拼装 ─────────────────────────────────────────────────
    @staticmethod
    def _child_env() -> dict[str, str]:
        """构造 yt-dlp 子进程的环境变量。"""
        env = dict(os.environ)
        # 强制子进程用 UTF-8 输出，否则 Windows 上按 cp936 编码，中文标题会乱码
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # yt-dlp 靠这几个变量定位浏览器的 cookie 数据库。某些宿主环境
        # （服务进程、沙箱、被裁剪过的 shell）不提供它们，会导致
        # --cookies-from-browser 直接报「找不到 cookie 数据库」——
        # 真正的用户环境里本来就有，所以这里补齐即可。
        home = Path.home()
        for key, value in (
            ("LOCALAPPDATA", home / "AppData" / "Local"),
            ("APPDATA", home / "AppData" / "Roaming"),
            ("USERPROFILE", home),
        ):
            if not env.get(key):
                env[key] = str(value)
        return env

    @staticmethod
    def detect_browsers() -> list[str]:
        """探测本机有哪些浏览器能拿来读 cookie（--cookies-from-browser 用）。"""
        from .cookies import find_browsers

        return [b.key for b in find_browsers() if b.installed]

    def _cookie_args(self, url: str = "") -> list[str]:
        """cookie 相关参数，**按目标域名决定发不发**。

        cookie 是站点特定的：抖音/西瓜不带就下不了，而 YouTube 带了别的浏览器
        会话的 cookie 会被判风控（"The page needs to be reloaded"）。实测过，
        全局无脑下发 cookie 会把本来能下的 YouTube 弄挂。
        """
        if not self._cookies_wanted(url):
            return []
        if self.cookies_file:
            path = Path(self.cookies_file)
            if path.exists():
                return ["--cookies", str(path)]
        if self.cookies_from_browser:
            return ["--cookies-from-browser", self.cookies_from_browser]
        return []

    def _cookies_wanted(self, url: str) -> bool:
        if not (self.cookies_file or self.cookies_from_browser):
            return False
        mode = str(self.config.get("download.cookie_mode", "auto") or "auto").lower()
        if mode == "never":
            return False
        if mode == "always":
            return True
        # auto：只给 cookie_domains 里匹配到的站点带
        domains = self.config.get("download.cookie_domains", []) or []
        if not domains:
            return True          # 没配白名单就退回老行为，别把用户搞糊涂
        host = linkparse.host_of(url)
        if not host:
            return True
        return any(host == d or host.endswith("." + d)
                   for d in (str(x).lower().lstrip(".") for x in domains))

    def _cookie_args_for(self, url: str) -> list[str]:
        """按 URL 拿 cookie 参数（带缓存，避免每个 URL 重复判断）。"""
        return self._cookie_args(url)

    def _common_args(self, url: str = "") -> list[str]:
        args = ["--no-playlist", "--no-warnings", "--no-color", "--ignore-config"]
        if self.proxy:
            args += ["--proxy", self.proxy]
        args += self._cookie_args(url)
        if self.max_filesize_mb > 0:
            args += ["--max-filesize", f"{self.max_filesize_mb}M"]
        ffmpeg = self.find_ffmpeg()
        if ffmpeg:
            # 显式告知位置：托管工具装的 ffmpeg 常常不在子进程的 PATH 里
            args += ["--ffmpeg-location", ffmpeg]
        args += ["--retries", "3", "--fragment-retries", "3", "--socket-timeout", "20"]
        return args + self.extra_args

    def _format_chains(self) -> list[list[str]]:
        """按优先级给出多套 ``-f`` 参数，前一套失败就换下一套。

        为什么需要降级链：只给一套选择器时，遇到「站点只有 DASH 分离流」或
        「不存在符合画质要求的格式」就会直接失败。多备几套能显著提高成功率。

        为什么要有「兼容编码」那一档：YouTube 现在默认给 AV1 + Opus，
        但交付给客户的文件得能在任意设备上播 —— 老手机、部分播放器、
        剪映之类对 AV1/Opus 支持很差。所以优先挑 H.264 + AAC，
        挑不到再退回站点给的最佳编码。
        """
        merge = self.has_ffmpeg()
        height = {"1080": 1080, "720": 720, "480": 480, "360": 360}.get(self.quality)
        compatible = bool(self.config.get("download.prefer_compatible", True))
        h = f"[height<={height}]" if height else ""
        chains: list[list[str]] = []

        if merge:
            if compatible:
                # avc1 = H.264，mp4a = AAC：这两样到哪都能播
                chains.append([
                    "-f",
                    f"bv*[vcodec^=avc1]{h}+ba[acodec^=mp4a]/"
                    f"bv*[vcodec^=avc1]{h}+ba/b{h}/"
                    f"bv*{h}+ba/b{h}",
                    "--merge-output-format", "mp4",
                ])
            # 有 ffmpeg：优先「视频轨 + 音频轨」合并，这是现代平台的唯一高清途径
            if height:
                chains.append(["-f", f"bv*{h}+ba/b{h}/b",
                               "--merge-output-format", "mp4"])
            chains.append(["-f", "bv*+ba/b", "--merge-output-format", "mp4"])
            # 有些站点没有 combined 格式，只有分离流
            chains.append(["-f", "bv*+ba", "--merge-output-format", "mp4"])
            chains.append(["-f", "b"])
        else:
            # 没有 ffmpeg：只能要「自带音频」的单个文件流
            if height:
                chains.append(["-f", f"b[height<={height}][ext=mp4]/b[height<={height}]/b"])
            chains.append(["-f", "b[ext=mp4]/b"])
            chains.append(["-f", "b[vcodec!=none][acodec!=none]/b"])
        return chains

    # ── 第一步：探测元数据 ─────────────────────────────────────────
    def probe(self, url: str) -> dict:
        """拿标题/时长/体积。失败返回 {}，不抛异常。"""
        base = self._resolve_base_cmd()
        if base is None:
            return {}
        cmd = base + ["-J", "--skip-download", "--no-warnings", "--no-playlist",
                      "--ignore-config"]
        if self.proxy:
            cmd += ["--proxy", self.proxy]
        cmd += self._cookie_args(url)
        cmd.append(url)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120, env=self._child_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        if proc.returncode != 0:
            return {}
        raw = (proc.stdout or "").strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if 0 <= start < end:
                try:
                    return json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    return {}
            return {}

    # ── 第二步：真正下载 ───────────────────────────────────────────
    def download(self, url: str, job_dir: Path, *,
                 title_hint: str = "",
                 progress: ProgressCallback = _noop,
                 cancel: threading.Event | None = None) -> DownloadResult:
        started = time.time()
        base = self._resolve_base_cmd()
        if base is None:
            return DownloadResult(False, engine=self.name,
                                  error="未安装 yt-dlp（pip install yt-dlp）")

        job_dir = Path(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)

        meta = self.probe(url)
        # 平台给的 title 可能是「标题 + 换行 + 整段简介」，先收拾干净
        title_maxlen = int(self.config.get("download.title_maxlen", 60) or 60)
        title = clean_media_title(str(meta.get("title") or ""), title_maxlen)
        title = title or title_hint or "video"
        maxlen = int(self.config.get("download.filename_maxlen", 60) or 60)
        # '%' 在 yt-dlp 的输出模板里有特殊含义，这里直接剔除，避免踩坑
        safe = sanitize_filename(title, maxlen=maxlen).replace("%", "")
        #: 用「安全标题.%(ext)s」当模板，让 yt-dlp 决定扩展名
        template = str(job_dir / f"{safe}.%(ext)s")

        progress(Progress(percent=0.0, stage="启动下载", total=self._estimate_size(meta)))

        chains = self._format_chains()
        last: DownloadResult | None = None
        for attempt, fmt_args in enumerate(chains, 1):
            stage = ("下载中" if attempt == 1
                     else f"下载中（换用备用格式 {attempt}/{len(chains)}）")
            # 上一次尝试可能留下半截文件，会干扰 _locate_output 的「取最大文件」，
            # 所以在每次尝试前清空 —— 这个目录本来就是这个订单独占的。
            if attempt > 1:
                self._clean_job_dir(job_dir)

            result, raw_log = self._run_once(
                base, url, job_dir, template, fmt_args, title, meta,
                progress, cancel, started, stage,
            )
            if result.ok or (cancel is not None and cancel.is_set()):
                return result
            last = result
            # 只有「格式不匹配」才值得换下一套；网络/登录类错误换格式也是白换
            if not _FORMAT_RETRYABLE.search(raw_log):
                break

        return last or DownloadResult(False, engine=self.name,
                                      error="没有可用的格式选择器")

    def _run_once(self, base: list[str], url: str, job_dir: Path, template: str,
                  fmt_args: list[str], title: str, meta: dict,
                  progress: ProgressCallback, cancel: threading.Event | None,
                  started: float, stage: str) -> tuple[DownloadResult, str]:
        """跑一次 yt-dlp。返回 (结果, 原始输出)，原始输出供上层判断能否换格式重试。"""
        # 刻意不加 --no-part：留 .part 后缀反而好 —— _locate_output 会把它排除掉，
        # 半截文件就绝不会被当成成品交付。
        cmd = base + ["--newline", "--progress", "--no-mtime", "-o", template]
        cmd += self._common_args(url)
        cmd += fmt_args
        cmd.append(url)

        log: list[str] = []
        final_path = ""
        killed_reason = ""

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=self._child_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            return DownloadResult(False, engine=self.name,
                                  error=f"无法启动 yt-dlp：{exc}"), ""

        def _kill(reason: str) -> None:
            nonlocal killed_reason
            if not killed_reason:
                killed_reason = reason
            try:
                proc.kill()
            except OSError:
                pass

        watchdog = threading.Timer(self.timeout, _kill, args=("timeout",))
        watchdog.daemon = True
        watchdog.start()

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                log.append(line)
                if len(log) > MAX_LOG_LINES:
                    del log[0]

                if cancel is not None and cancel.is_set():
                    _kill("cancelled")
                    break

                match = _YTDLP_PROGRESS_RE.search(line)
                if match:
                    pct = float(match.group("pct") or 0)
                    total = _to_bytes(match.group("size") or "0", match.group("sunit") or "B")
                    speed_raw = match.group("speed") or "0"
                    speed = 0.0
                    if not speed_raw.lower().startswith("unknown"):
                        speed = float(_to_bytes(speed_raw, match.group("spunit") or "B"))
                    progress(Progress(
                        percent=pct,
                        downloaded=int(total * pct / 100) if total else 0,
                        total=total,
                        speed=speed,
                        eta=(match.group("eta") or "").replace("Unknown", ""),
                        stage=stage,
                    ))
                    continue

                if line.startswith("[Merger]"):
                    progress(Progress(percent=100.0, stage="合并音视频"))
                elif line.startswith(("[ExtractAudio]", "[FixupM3u8]", "[Fixup")):
                    progress(Progress(percent=100.0, stage="后处理"))

                for pattern in _FINAL_PATH_PATTERNS:
                    found = pattern.search(line)
                    if found:
                        final_path = found.group(1).strip().strip('"')
                        break
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            _kill("timeout")
            proc.wait(timeout=10)
        finally:
            watchdog.cancel()
            try:
                if proc.stdout:
                    proc.stdout.close()
            except OSError:
                pass

        raw_log = "\n".join(log)
        elapsed = time.time() - started

        if killed_reason == "timeout":
            return DownloadResult(False, title=title, engine=self.name,
                                  duration_sec=elapsed,
                                  error=f"下载超时（超过 {int(self.timeout)} 秒）"), raw_log
        if killed_reason == "cancelled":
            return DownloadResult(False, title=title, engine=self.name,
                                  duration_sec=elapsed, error="任务已取消"), raw_log

        located = self._locate_output(job_dir, final_path)
        if located is None:
            return DownloadResult(
                False, title=title, engine=self.name, duration_sec=elapsed,
                error=self._explain_download_failure(proc.returncode, log, job_dir),
            ), raw_log

        size = located.stat().st_size
        if size <= 0:
            return DownloadResult(False, title=title, engine=self.name,
                                  duration_sec=elapsed,
                                  error="下载出的文件是空的"), raw_log

        return DownloadResult(
            ok=True, file_path=str(located), title=title, size_bytes=size,
            duration_sec=elapsed, engine=self.name,
            meta={
                "id": meta.get("id", ""),
                "duration": meta.get("duration"),
                "uploader": meta.get("uploader") or meta.get("creator") or "",
                "webpage_url": meta.get("webpage_url") or url,
                "ext": located.suffix.lstrip("."),
            },
        ), raw_log

    @staticmethod
    def _clean_job_dir(job_dir: Path) -> None:
        """换格式重试前清场，避免上一次的半截文件被误认为成品。"""
        for item in job_dir.iterdir():
            if not item.is_file():
                continue
            try:
                item.unlink()
            except OSError:
                pass

    @staticmethod
    def _estimate_size(meta: dict) -> int:
        if not meta:
            return 0
        for key in ("filesize", "filesize_approx"):
            if meta.get(key):
                return int(meta[key])
        return 0

    @staticmethod
    def _is_fragment(path: Path) -> bool:
        """是不是 yt-dlp 的 DASH 分片（视频轨/音频轨）而不是成品。"""
        return bool(_FRAGMENT_RE.search(path.name))

    @staticmethod
    def _fragments(job_dir: Path) -> list[Path]:
        try:
            return [p for p in job_dir.iterdir()
                    if p.is_file() and YtDlpEngine._is_fragment(p)]
        except OSError:
            return []

    @staticmethod
    def _locate_output(job_dir: Path, hint: str) -> Path | None:
        """定位下载产物：优先用 yt-dlp 报的路径，否则扫目录取最大的文件。

        **分片和 .part 都不算产物** —— 宁可返回 None 让上层报错，
        也不能把半截文件当成成品交付。
        """
        if hint:
            candidate = Path(hint)
            if not candidate.is_absolute():
                candidate = job_dir / candidate.name
            if (candidate.exists() and candidate.is_file()
                    and candidate.stat().st_size > 0
                    and not YtDlpEngine._is_fragment(candidate)):
                return candidate
        files = [
            p for p in job_dir.iterdir()
            if p.is_file()
            and not p.name.endswith(TEMP_SUFFIXES)
            and not YtDlpEngine._is_fragment(p)
            and p.stat().st_size > 0
        ]
        return max(files, key=lambda p: p.stat().st_size) if files else None

    def _explain_download_failure(self, returncode: int, log: list[str],
                                  job_dir: Path) -> str:
        """没产出成品时给一句人话。优先识别「合流失败」这个最隐蔽的坑。"""
        fragments = self._fragments(job_dir)
        if fragments:
            names = "、".join(p.name for p in fragments[:3])
            if not self.has_ffmpeg():
                return (f"只下到分离的音视频轨（{names}），没有可用的 ffmpeg 无法合流成"
                        f"完整文件。跑 `python run.py doctor` 看 ffmpeg 状态")
            return (f"下载完成但合流失败（{names}）。当前 ffmpeg："
                    f"{self.find_ffmpeg() or '未找到'}，建议换成官方静态版 ffmpeg")
        return self._explain(returncode, log)

    @staticmethod
    def _explain(returncode: int, log: list[str]) -> str:
        """把 yt-dlp 的原始输出翻译成一句人话。"""
        text = "\n".join(log)
        for pattern, hint in _ERROR_HINTS:
            if pattern.search(text):
                return hint
        # 挑出真正的 ERROR 行
        errors = [ln for ln in log if "ERROR" in ln.upper()]
        if errors:
            msg = errors[-1]
            msg = re.sub(r"^ERROR:\s*", "", msg, flags=re.IGNORECASE)
            return msg.strip()[:300]
        return f"yt-dlp 退出码 {returncode}，没有产出文件"


# ══════════════════════════════════════════════════════════════════════
#  直链 HTTP
# ══════════════════════════════════════════════════════════════════════
class HttpEngine:
    """直接 HTTP 下载，用于 ``.mp4`` / ``.webm`` 这类直链。"""

    name = "http"
    label = "直链下载"
    CHUNK = 256 * 1024

    def __init__(self, config: Config):
        self.config = config
        self.timeout = float(config.get("net.timeout_sec", 15))
        self.ua = str(config.get("net.user_agent", ""))
        self.max_bytes = int(config.get("download.max_filesize_mb", 0) or 0) * 1024 * 1024
        self.proxy = parse_proxy_spec(config.get("net.proxy"))
        self.verify_tls = bool(config.get("net.verify_tls", True))

    @property
    def available(self) -> bool:
        return True

    @staticmethod
    def guess_filename(url: str, content_disposition: str = "") -> str:
        """先看 Content-Disposition，再退回 URL 最后一段。"""
        if content_disposition:
            match = re.search(
                r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", content_disposition, re.I
            )
            if match:
                name = urllib.parse.unquote(match.group(1).strip())
                if name:
                    return name
        path = urllib.parse.urlsplit(url).path
        name = urllib.parse.unquote(os.path.basename(path))
        return name or "video.mp4"

    def download(self, url: str, job_dir: Path, *,
                 title_hint: str = "",
                 progress: ProgressCallback = _noop,
                 cancel: threading.Event | None = None) -> DownloadResult:
        started = time.time()
        job_dir = Path(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            resp = open_stream(url, timeout=self.timeout, proxy=self.proxy,
                               user_agent=self.ua, verify_tls=self.verify_tls)
        except HttpError as exc:
            return DownloadResult(False, engine=self.name, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - 网络异常兜底
            return DownloadResult(False, engine=self.name, error=f"{type(exc).__name__}: {exc}")

        try:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            content_type = headers.get("content-type", "").split(";")[0].strip().lower()
            total = int(headers.get("content-length") or 0)

            # 明显不是媒体的响应：告诉运营「这链接打开是网页」，而不是存一堆 HTML
            if content_type.startswith("text/html") or content_type.startswith("application/json"):
                return DownloadResult(
                    False, engine=self.name,
                    error=f"这个链接返回的是网页（{content_type}），不是视频文件 —— "
                          f"请用视频的原页面链接",
                )
            if self.max_bytes and total and total > self.max_bytes:
                return DownloadResult(
                    False, engine=self.name,
                    error=f"文件 {human_size(total)} 超过配置上限 "
                          f"{human_size(self.max_bytes)}",
                )

            raw_name = self.guess_filename(url, headers.get("content-disposition", ""))
            maxlen = int(self.config.get("download.filename_maxlen", 80) or 80)
            suffix = Path(raw_name).suffix or ".mp4"
            base_name = sanitize_filename(Path(raw_name).stem or title_hint or "video",
                                          maxlen=maxlen)
            dest = ensure_unique(job_dir / f"{base_name}{suffix}")

            progress(Progress(percent=0.0, total=total, stage="开始下载"))
            done = 0
            last_report = 0.0
            with open(dest, "wb") as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("任务已取消")
                    chunk = resp.read(self.CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if self.max_bytes and done > self.max_bytes:
                        raise ValueError(
                            f"文件超过配置上限 {human_size(self.max_bytes)}"
                        )
                    now = time.time()
                    if now - last_report >= 0.5:
                        last_report = now
                        elapsed = max(0.001, now - started)
                        progress(Progress(
                            percent=(done / total * 100) if total else 0.0,
                            downloaded=done, total=total,
                            speed=done / elapsed, stage="下载中",
                        ))
        except InterruptedError as exc:
            return DownloadResult(False, engine=self.name, error=str(exc),
                                  duration_sec=time.time() - started)
        except ValueError as exc:
            return DownloadResult(False, engine=self.name, error=str(exc),
                                  duration_sec=time.time() - started)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(False, engine=self.name,
                                  error=f"下载中断：{type(exc).__name__}: {exc}",
                                  duration_sec=time.time() - started)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

        size = dest.stat().st_size
        if size <= 0:
            return DownloadResult(False, engine=self.name, error="下载出的文件是空的")
        progress(Progress(percent=100.0, downloaded=size, total=size, stage="完成"))
        return DownloadResult(
            ok=True, file_path=str(dest), title=title_hint or dest.stem,
            size_bytes=size, duration_sec=time.time() - started, engine=self.name,
        )


# ══════════════════════════════════════════════════════════════════════
#  门面
# ══════════════════════════════════════════════════════════════════════
class Downloader:
    """按能力择优选引擎：能上 yt-dlp 就上，直链才退到 HTTP。"""

    def __init__(self, config: Config):
        self.config = config
        self.ytdlp = YtDlpEngine(config)
        self.http = HttpEngine(config)
        self.prefer_ytdlp = bool(config.get("download.prefer_ytdlp", True))

    def available_engines(self) -> list[str]:
        out = []
        if self.ytdlp.available:
            extra = "（含 ffmpeg，可合流高清）" if self.ytdlp.has_ffmpeg() else "（无 ffmpeg，仅单文件流）"
            out.append(f"yt-dlp {self.ytdlp.version or ''}{extra}".strip())
        else:
            out.append("yt-dlp 未安装")
        out.append("直链下载 可用")
        return out

    def download(self, url: str, job_dir: Path, *, kind: str = "page",
                 title_hint: str = "",
                 progress: ProgressCallback = _noop,
                 cancel: threading.Event | None = None) -> DownloadResult:
        """两级降级：先 yt-dlp（能处理视频页/合流/HLS），再直链 HTTP。

        直链的 ``.mp4`` 用 yt-dlp 也是对的（它内部走 HTTP 并顺带拿元数据），
        所以这里不必按 kind 分叉，一路降级到底即可。
        """
        attempts: list[str] = []
        cancelled = lambda: cancel is not None and cancel.is_set()  # noqa: E731

        if self.prefer_ytdlp and self.ytdlp.available:
            result = self.ytdlp.download(url, job_dir, title_hint=title_hint,
                                         progress=progress, cancel=cancel)
            if result.ok or cancelled():
                return result
            attempts.append(f"yt-dlp：{result.error}")

        result = self.http.download(url, job_dir, title_hint=title_hint,
                                    progress=progress, cancel=cancel)
        if result.ok or cancelled():
            return result
        attempts.append(f"直链：{result.error}")

        if not self.ytdlp.available:
            attempts.append("yt-dlp 未安装（pip install yt-dlp 后才能解析视频页面）")
        elif not self.prefer_ytdlp:
            attempts.insert(0, "配置里关闭了 prefer_ytdlp")

        return DownloadResult(False, engine="", error="；".join(attempts))


def job_dir_for(root: str | Path, order_id: int | str) -> Path:
    """每个订单一个独立目录，避免并发下载时互相串文件。"""
    path = Path(root) / str(order_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
