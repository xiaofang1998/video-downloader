"""浏览器 cookie 的探测与读取。

抖音、小红书这类站点有风控，yt-dlp 必须带上浏览器 cookie 才能解析。
这个模块负责：找到浏览器 profile → 读出里面有哪些站点的 cookie →
告诉用户「配好了没」。

只读 host_key / name 这类元信息用于**计数和判断有没有**，
cookie 值由 yt-dlp 自己去解密，本模块不碰。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: 支持的站点：key → (中文名, 登录入口, 域名匹配片段)
SITES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "douyin": ("抖音", "https://www.douyin.com/", ("douyin.com", "iesdouyin.com")),
    "xiaohongshu": ("小红书", "https://www.xiaohongshu.com/",
                    ("xiaohongshu.com", "xhslink.com")),
    "kuaishou": ("快手", "https://www.kuaishou.com/", ("kuaishou.com",)),
    "weibo": ("微博", "https://weibo.com/", ("weibo.com", "weibo.cn")),
    "bilibili": ("哔哩哔哩", "https://www.bilibili.com/", ("bilibili.com", "b23.tv")),
    "tiktok": ("TikTok", "https://www.tiktok.com/", ("tiktok.com",)),
    "instagram": ("Instagram", "https://www.instagram.com/", ("instagram.com",)),
}

#: 浏览器 → 可能的可执行文件路径（Windows）
BROWSER_EXES: dict[str, tuple[str, ...]] = {
    "edge": (
        r"{PROGRAMFILES(X86)}\Microsoft\Edge\Application\msedge.exe",
        r"{PROGRAMFILES}\Microsoft\Edge\Application\msedge.exe",
        r"{LOCALAPPDATA}\Microsoft\Edge\Application\msedge.exe",
    ),
    "chrome": (
        r"{PROGRAMFILES}\Google\Chrome\Application\chrome.exe",
        r"{PROGRAMFILES(X86)}\Google\Chrome\Application\chrome.exe",
        r"{LOCALAPPDATA}\Google\Chrome\Application\chrome.exe",
    ),
    "brave": (
        r"{PROGRAMFILES}\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"{LOCALAPPDATA}\BraveSoftware\Brave-Browser\Application\brave.exe",
    ),
    "vivaldi": (r"{LOCALAPPDATA}\Vivaldi\Application\vivaldi.exe",),
    "firefox": (
        r"{PROGRAMFILES}\Mozilla Firefox\firefox.exe",
        r"{PROGRAMFILES(X86)}\Mozilla Firefox\firefox.exe",
    ),
}

#: 浏览器 → User Data 根目录
BROWSER_DATA: dict[str, str] = {
    "edge": r"{LOCALAPPDATA}\Microsoft\Edge\User Data",
    "chrome": r"{LOCALAPPDATA}\Google\Chrome\User Data",
    "brave": r"{LOCALAPPDATA}\BraveSoftware\Brave-Browser\User Data",
    "vivaldi": r"{LOCALAPPDATA}\Vivaldi\User Data",
    "firefox": r"{APPDATA}\Mozilla\Firefox\Profiles",
}

#: yt-dlp 的 --cookies-from-browser 认的名字，映射到我们的 key
YTDLP_NAMES = {
    "edge": "edge", "chrome": "chrome", "brave": "brave",
    "vivaldi": "vivaldi", "firefox": "firefox",
}


def _vars() -> dict[str, str]:
    """环境变量兜底。

    被裁剪过的宿主环境（服务进程、沙箱）可能没有 APPDATA / LOCALAPPDATA，
    这时用 Path.home() 推出来 —— 否则后面拼路径会得到 "\\Microsoft\\Edge\\..."。
    """
    home = Path.home()
    local = home / "AppData" / "Local"
    roaming = home / "AppData" / "Roaming"
    return {
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA") or str(local),
        "APPDATA": os.environ.get("APPDATA") or str(roaming),
        "PROGRAMFILES": os.environ.get("ProgramFiles") or r"C:\Program Files",
        "PROGRAMFILES(X86)": (os.environ.get("ProgramFiles(x86)")
                              or r"C:\Program Files (x86)"),
    }


def _expand(template: str) -> Path:
    out = template
    values = _vars()
    # 先替换带括号的长名字，避免被 PROGRAMFILES 抢先匹配
    for key in ("PROGRAMFILES(X86)", "LOCALAPPDATA", "PROGRAMFILES", "APPDATA"):
        out = out.replace("{" + key + "}", values[key])
    return Path(out)


@dataclass
class BrowserInfo:
    """一个可用的浏览器。"""

    key: str
    label: str
    exe: Path | None
    data_dir: Path | None
    profiles: list[Path] = field(default_factory=list)

    @property
    def installed(self) -> bool:
        return self.exe is not None and self.exe.exists()

    @property
    def ytdlp_name(self) -> str:
        return YTDLP_NAMES.get(self.key, self.key)

    @property
    def app_bound(self) -> bool:
        """这个浏览器是否启用了 App-Bound 加密。

        启用了的话，新写入的 cookie 是 ``v20``，**yt-dlp 解不开**
        （它的源码里完全没有相关支持）。此时应该换一个没启用的浏览器，
        或者改用导出 cookies.txt 的方式。
        """
        if self.data_dir is None:
            return False
        state = self.data_dir / "Local State"
        if not state.exists():
            return False
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return False
        return "app_bound_encrypted_key" in (data.get("os_crypt") or {})

    def describe(self) -> str:
        if not self.installed:
            return f"{self.label}（未安装）"
        note = "  ⚠ 启用了 App-Bound 加密，cookie 读不出来" if self.app_bound else ""
        return f"{self.label}（{len(self.profiles)} 个配置）{note}"


_LABELS = {
    "edge": "Edge", "chrome": "Chrome", "brave": "Brave",
    "vivaldi": "Vivaldi", "firefox": "Firefox",
}


def find_browsers() -> list[BrowserInfo]:
    """探测本机所有可用的浏览器。"""
    out: list[BrowserInfo] = []
    for key, templates in BROWSER_EXES.items():
        exe = next((_expand(t) for t in templates if _expand(t).exists()), None)
        data_dir = _expand(BROWSER_DATA[key]) if key in BROWSER_DATA else None
        profiles: list[Path] = []
        if data_dir is not None and data_dir.is_dir():
            if key == "firefox":
                profiles = [p for p in data_dir.iterdir()
                            if p.is_dir() and (p / "cookies.sqlite").exists()]
            else:
                profiles = sorted(
                    p for p in data_dir.iterdir()
                    if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile "))
                )
        out.append(BrowserInfo(key, _LABELS.get(key, key), exe, data_dir, profiles))
    return out


def _cookie_db(profile: Path) -> Path | None:
    for rel in ("Network/Cookies", "Cookies", "cookies.sqlite"):
        candidate = profile / rel
        if candidate.exists():
            return candidate
    return None


@dataclass
class CookieScan:
    """一次 profile 扫描的结果。"""

    hosts: dict[str, int] = field(default_factory=dict)
    #: 加密方案 → 条数。v10 = 老式 DPAPI（yt-dlp 能解）；
    #: v20 = App-Bound（Chrome/Edge 127+，yt-dlp 解不开）
    versions: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def app_bound(self) -> int:
        return self.versions.get("v20", 0)


def _is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 32


def read_hosts(profile: Path) -> CookieScan:
    """读出一个 profile 里各站点有多少 cookie，以及它们用的加密方案。

    浏览器在运行时数据库会被锁住（Windows 上是独占锁），这时返回提示信息。
    """
    db = _cookie_db(profile)
    if db is None:
        return CookieScan(error="这个配置里没有 cookie 数据库")

    tmpdir = Path(tempfile.mkdtemp(prefix="xydl-ck-"))
    try:
        target = tmpdir / db.name
        # 浏览器运行时 SQLite 会被锁，复制一份再读
        shutil.copy2(db, target)
        for suffix in ("-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(target) + suffix))

        con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            if db.name == "cookies.sqlite":     # Firefox：明文，没有加密问题
                rows = con.execute(
                    "SELECT host, COUNT(*) FROM moz_cookies GROUP BY host"
                ).fetchall()
                return CookieScan(hosts=dict(rows), versions={"plain": sum(dict(rows).values())})

            rows = con.execute(
                "SELECT host_key, CAST(substr(encrypted_value, 1, 3) AS TEXT) AS v, "
                "COUNT(*) FROM cookies GROUP BY host_key, v"
            ).fetchall()
        finally:
            con.close()

        hosts: dict[str, int] = {}
        versions: dict[str, int] = {}
        for host, prefix, count in rows:
            hosts[host] = hosts.get(host, 0) + count
            key = (prefix or "?").strip() or "?"
            versions[key] = versions.get(key, 0) + count
        return CookieScan(hosts=hosts, versions=versions)
    except OSError as exc:
        if _is_locked_error(exc):
            return CookieScan(error="浏览器正在运行，cookie 数据库被锁住了 —— 请完全关闭浏览器")
        return CookieScan(error=f"{type(exc).__name__}: {exc}")
    except sqlite3.Error as exc:
        return CookieScan(error=f"cookie 数据库读取失败：{exc}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def count_for_site(hosts: dict[str, int], site_key: str) -> int:
    """某个站点的 cookie 条数。

    必须按**域名后缀**匹配，不能用子串 —— 否则 ``notdouyin.com`` 会被
    算成抖音的，虚报一个「已就绪」然后下载仍然失败。
    """
    if site_key not in SITES:
        return 0
    fragments = SITES[site_key][2]
    total = 0
    for host, count in hosts.items():
        host = host.lower().lstrip(".")
        if any(host == frag or host.endswith("." + frag) for frag in fragments):
            total += count
    return total


@dataclass
class CookieReport:
    """一次「这个浏览器里有没有目标站点 cookie」的检查结论。"""

    browser: str
    profile: Path
    counts: dict[str, int] = field(default_factory=dict)
    hosts: dict[str, int] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def app_bound(self) -> int:
        """App-Bound（v20）加密的 cookie 条数 —— 这些 yt-dlp 解不开。"""
        return self.versions.get("v20", 0)

    @property
    def decryptable(self) -> bool:
        """cookie 里是否存在 yt-dlp 能解密的那些。"""
        return self.app_bound == 0 or bool(self.versions.get("v10")
                                           or self.versions.get("plain"))

    def for_site(self, site_key: str) -> int:
        return self.counts.get(site_key, 0)

    def warn(self) -> str:
        """如果 cookie 用了 yt-dlp 解不开的加密，给一句提示。"""
        if self.error or not self.app_bound:
            return ""
        if self.versions.get("v10") or self.versions.get("plain"):
            return (f"部分 cookie 是 App-Bound 加密（v20×{self.app_bound}），"
                    f"yt-dlp 可能只能解出其中一部分")
        return (f"cookie 全部是 App-Bound 加密（v20×{self.app_bound}），"
                f"yt-dlp **解不开** —— 请改用 cookies.txt 方式")


def inspect(site_keys: list[str] | None = None) -> list[CookieReport]:
    """检查所有浏览器的所有 profile，看目标站点的 cookie 是否有货。"""
    site_keys = site_keys or list(SITES)
    reports: list[CookieReport] = []
    for browser in find_browsers():
        if not browser.profiles:
            continue
        for profile in browser.profiles:
            scan = read_hosts(profile)
            report = CookieReport(browser=browser.key, profile=profile,
                                  hosts=scan.hosts, versions=scan.versions,
                                  error=scan.error)
            report.counts = {key: count_for_site(scan.hosts, key) for key in site_keys}
            reports.append(report)
    return reports


def pick_best(site_key: str, reports: list[CookieReport] | None = None
              ) -> CookieReport | None:
    """挑出含目标站点 cookie 最多的那个 profile。"""
    reports = reports if reports is not None else inspect([site_key])
    usable = [r for r in reports if r.ok and r.for_site(site_key) > 0]
    if not usable:
        return None
    return max(usable, key=lambda r: r.for_site(site_key))


def ytdlp_spec(browser_key: str, profile: Path) -> str:
    """构造 --cookies-from-browser 的值，直接指向 profile 目录。

    yt-dlp 的语法是 ``BROWSER[:PROFILE]``，PROFILE 也可以是目录路径。
    用绝对路径最稳 —— 不用管 profile 叫什么名字。
    """
    return f"{YTDLP_NAMES.get(browser_key, browser_key)}:{profile}"
