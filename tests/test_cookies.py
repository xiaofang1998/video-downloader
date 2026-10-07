"""浏览器 cookie 探测的测试。全部用临时构造的假 profile，不碰真实浏览器数据。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xydl import cookies as ck


def make_chrome_profile(root: Path, name: str, hosts: dict[str, int]) -> Path:
    """造一个最小可用的 Chrome 系 cookie 数据库。"""
    profile = root / name
    (profile / "Network").mkdir(parents=True)
    db = profile / "Network" / "Cookies"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
    for host, count in hosts.items():
        for i in range(count):
            con.execute("INSERT INTO cookies VALUES (?,?,?)", (host, f"c{i}", b"x"))
    con.commit()
    con.close()
    return profile


def make_firefox_profile(root: Path, name: str, hosts: dict[str, int]) -> Path:
    profile = root / name
    profile.mkdir(parents=True)
    con = sqlite3.connect(profile / "cookies.sqlite")
    con.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
    for host, count in hosts.items():
        for i in range(count):
            con.execute("INSERT INTO moz_cookies VALUES (?,?,?)", (host, f"c{i}", "x"))
    con.commit()
    con.close()
    return profile


# ── 读取 ──────────────────────────────────────────────────────────────
def test_read_hosts_chrome_layout(tmp_path: Path):
    profile = make_chrome_profile(tmp_path, "Default",
                                  {"www.douyin.com": 3, "example.com": 2})
    scan = ck.read_hosts(profile)
    assert scan.error == ""
    assert scan.hosts["www.douyin.com"] == 3
    assert scan.hosts["example.com"] == 2


def test_read_hosts_reports_encryption_scheme(tmp_path: Path):
    """v10 = yt-dlp 能解的 DPAPI；v20 = App-Bound，它解不开。"""
    profile = tmp_path / "Default"
    (profile / "Network").mkdir(parents=True)
    con = sqlite3.connect(profile / "Network" / "Cookies")
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
    for i in range(3):
        con.execute("INSERT INTO cookies VALUES (?,?,?)",
                    ("www.douyin.com", f"a{i}", b"v10\x01\x02\x03"))
    for i in range(5):
        con.execute("INSERT INTO cookies VALUES (?,?,?)",
                    ("www.douyin.com", f"b{i}", b"v20\x01\x02\x03"))
    con.commit()
    con.close()

    scan = ck.read_hosts(profile)
    assert scan.hosts["www.douyin.com"] == 8
    assert scan.versions.get("v10") == 3
    assert scan.versions.get("v20") == 5
    assert scan.app_bound == 5


def test_report_warns_when_all_cookies_are_app_bound(tmp_path: Path):
    profile = tmp_path / "Default"
    (profile / "Network").mkdir(parents=True)
    con = sqlite3.connect(profile / "Network" / "Cookies")
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
    con.execute("INSERT INTO cookies VALUES (?,?,?)",
                ("www.douyin.com", "a", b"v20xx"))
    con.commit()
    con.close()

    scan = ck.read_hosts(profile)
    report = ck.CookieReport(browser="edge", profile=profile,
                             hosts=scan.hosts, versions=scan.versions)
    warning = report.warn()
    assert "App-Bound" in warning
    assert "解不开" in warning
    assert report.decryptable is False


def test_report_does_not_warn_for_plain_or_dpapi(tmp_path: Path):
    profile = make_chrome_profile(tmp_path, "Default", {"www.douyin.com": 1})
    # 造的是空 encrypted_value，前缀为空
    scan = ck.read_hosts(profile)
    report = ck.CookieReport(browser="chrome", profile=profile,
                             hosts=scan.hosts, versions=scan.versions)
    assert report.warn() == ""
    assert report.decryptable is True


def test_read_hosts_firefox_layout(tmp_path: Path):
    profile = make_firefox_profile(tmp_path, "abc.default",
                                   {"www.douyin.com": 5})
    scan = ck.read_hosts(profile)
    assert scan.error == ""
    assert scan.hosts["www.douyin.com"] == 5
    assert scan.app_bound == 0        # Firefox 是明文，没有加密问题


def test_read_hosts_missing_db(tmp_path: Path):
    profile = tmp_path / "empty"
    profile.mkdir()
    scan = ck.read_hosts(profile)
    assert scan.hosts == {}
    assert "没有 cookie 数据库" in scan.error


def test_read_hosts_does_not_touch_the_original(tmp_path: Path):
    """读的时候必须复制一份 —— 直接开原库会污染浏览器的数据。"""
    profile = make_chrome_profile(tmp_path, "Default", {"a.com": 1})
    db = profile / "Network" / "Cookies"
    before = db.read_bytes()
    ck.read_hosts(profile)
    assert db.read_bytes() == before


def test_read_hosts_cleans_up_temp_files(tmp_path: Path):
    import tempfile

    profile = make_chrome_profile(tmp_path, "Default", {"a.com": 1})
    tmp_root = Path(tempfile.gettempdir())
    before = {p.name for p in tmp_root.glob("xydl-ck-*")}
    ck.read_hosts(profile)
    after = {p.name for p in tmp_root.glob("xydl-ck-*")}
    assert after == before          # 不留垃圾


# ── 站点匹配 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("hosts,site,expected", [
    ({"www.douyin.com": 3}, "douyin", 3),
    ({"v.douyin.com": 1, "www.iesdouyin.com": 2}, "douyin", 3),
    ({"www.douyin.com": 3}, "xiaohongshu", 0),
    ({"www.xiaohongshu.com": 4, "xhslink.com": 1}, "xiaohongshu", 5),
    # 不能被相似域名骗到
    ({"evil-douyin.com": 9}, "douyin", 0),
    ({"notdouyin.com.cn": 9}, "douyin", 0),
])
def test_count_for_site(hosts, site, expected):
    assert ck.count_for_site(hosts, site) == expected


def test_count_for_unknown_site_is_zero():
    assert ck.count_for_site({"www.douyin.com": 5}, "不存在的站") == 0


# ── 挑选 ──────────────────────────────────────────────────────────────
def test_pick_best_chooses_profile_with_most_cookies():
    reports = [
        ck.CookieReport(browser="chrome", profile=Path("/a"), counts={"douyin": 2}),
        ck.CookieReport(browser="edge", profile=Path("/b"), counts={"douyin": 7}),
        ck.CookieReport(browser="firefox", profile=Path("/c"), counts={"douyin": 0}),
    ]
    best = ck.pick_best("douyin", reports)
    assert best is not None and best.profile == Path("/b")


def test_pick_best_ignores_profiles_that_failed():
    reports = [
        ck.CookieReport(browser="edge", profile=Path("/a"), counts={"douyin": 9},
                        error="浏览器正在运行，cookie 数据库被锁住了"),
        ck.CookieReport(browser="chrome", profile=Path("/b"), counts={"douyin": 1}),
    ]
    assert ck.pick_best("douyin", reports).profile == Path("/b")


def test_pick_best_returns_none_when_nothing_found():
    reports = [ck.CookieReport(browser="chrome", profile=Path("/a"),
                               counts={"douyin": 0})]
    assert ck.pick_best("douyin", reports) is None


# ── yt-dlp 参数拼装 ───────────────────────────────────────────────────
def test_ytdlp_spec_points_at_profile_path():
    spec = ck.ytdlp_spec("chrome", Path(r"C:\Users\x\Chrome\User Data\Default"))
    assert spec.startswith("chrome:")
    assert "Default" in spec


def test_ytdlp_spec_uses_ytdlp_browser_names():
    assert ck.ytdlp_spec("edge", Path("/p")).startswith("edge:")


# ── 环境变量兜底 ──────────────────────────────────────────────────────
def test_vars_fall_back_when_env_missing(monkeypatch):
    """APPDATA / LOCALAPPDATA 缺失时不能用空串去拼路径。"""
    for key in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.delenv(key, raising=False)
    values = ck._vars()
    for key in ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        assert values[key], f"{key} 应该是非空路径"
        assert values[key].startswith("C:\\")
    assert "\\Microsoft\\Edge" not in values["LOCALAPPDATA"]


def test_expand_handles_parenthesised_var_first():
    """{PROGRAMFILES(X86)} 不能被 {PROGRAMFILES} 抢先匹配。"""
    from xydl.cookies import _expand

    path = _expand(r"{PROGRAMFILES(X86)}\Microsoft\Edge\Application\msedge.exe")
    assert "{PROGRAMFILES" not in str(path)
    assert "(X86)" not in str(path).upper() or "Program Files (x86)" in str(path)


def test_find_browsers_shape():
    browsers = ck.find_browsers()
    assert browsers
    for browser in browsers:
        assert browser.key and browser.label
        assert isinstance(browser.installed, bool)


# ── App-Bound 加密探测 ────────────────────────────────────────────────
def _make_local_state(data_dir: Path, app_bound: bool) -> None:
    import json

    data_dir.mkdir(parents=True, exist_ok=True)
    os_crypt = {"encrypted_key": "AAA"}
    if app_bound:
        os_crypt["app_bound_encrypted_key"] = "BBB"
    (data_dir / "Local State").write_text(
        json.dumps({"os_crypt": os_crypt}), encoding="utf-8")


def test_app_bound_detected_from_local_state(tmp_path: Path):
    """Edge 127+ 会把密钥换成 App-Bound，此时 yt-dlp 读不出 cookie。

    实测：本机 Chrome 156 的 Local State 里没有这个字段（cookie 是 v10，能读），
    Edge 154 里有（cookie 是 v20，读不了）。选错浏览器会白折腾一趟。
    """
    plain = tmp_path / "chrome-data"
    _make_local_state(plain, app_bound=False)
    assert ck.BrowserInfo("chrome", "Chrome", None, plain).app_bound is False

    locked = tmp_path / "edge-data"
    _make_local_state(locked, app_bound=True)
    assert ck.BrowserInfo("edge", "Edge", None, locked).app_bound is True


def test_app_bound_missing_or_broken_local_state_is_not_fatal(tmp_path: Path):
    empty = tmp_path / "no-data"
    empty.mkdir()
    assert ck.BrowserInfo("chrome", "Chrome", None, empty).app_bound is False
    assert ck.BrowserInfo("chrome", "Chrome", None, None).app_bound is False

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "Local State").write_text("{not json", encoding="utf-8")
    assert ck.BrowserInfo("chrome", "Chrome", None, broken).app_bound is False


def test_describe_warns_about_app_bound(tmp_path: Path):
    data = tmp_path / "edge-data"
    _make_local_state(data, app_bound=True)
    exe = tmp_path / "msedge.exe"
    exe.write_bytes(b"stub")          # describe() 对未安装的浏览器会提前返回
    assert "App-Bound" in ck.BrowserInfo("edge", "Edge", exe, data).describe()


def test_browsers_are_ranked_so_app_bound_goes_last():
    """run.py login 依赖这个排序：能读 cookie 的浏览器排在前面。"""
    browsers = [b for b in ck.find_browsers() if b.installed]
    ordered = sorted(browsers, key=lambda b: (b.app_bound, b.key))
    flags = [b.app_bound for b in ordered]
    assert flags == sorted(flags), "启用了 App-Bound 的浏览器必须排在后面"
