"""系统代理解析测试。

核心要守住的回归：Windows 上「跟随系统代理」必须**优先读注册表**，
而不是 `urllib.request.getproxies()` 读到的环境变量。

血泪教训：环境变量里残留过一个已死端口的代理（`127.0.0.1:60387`），
而真正在听的 Clash 是 7897。urllib 读代理时环境变量优先于注册表，
于是 yt-dlp 走 60387 → 隧道 502 → Instagram 报「failed to fetch」。
"""

from __future__ import annotations

import urllib.request

from xydl.http import _parse_proxy_server, parse_proxy_spec, system_proxy_url


def test_parse_proxy_server_mixed_port():
    assert _parse_proxy_server("127.0.0.1:7897") == "http://127.0.0.1:7897"


def test_parse_proxy_server_already_has_scheme():
    assert _parse_proxy_server("http://127.0.0.1:7897") == "http://127.0.0.1:7897"


def test_parse_proxy_server_protocol_list_prefers_https():
    raw = "http=127.0.0.1:7890;https=127.0.0.1:7891;socks=127.0.0.1:7892"
    assert _parse_proxy_server(raw) == "http://127.0.0.1:7891"


def test_parse_proxy_server_empty():
    assert _parse_proxy_server("") == ""
    assert _parse_proxy_server("   ") == ""


def test_parse_proxy_spec_none_and_off():
    assert parse_proxy_spec("none") == ""
    assert parse_proxy_spec("direct") == ""
    assert parse_proxy_spec("http://127.0.0.1:7890") == "http://127.0.0.1:7890"


def test_system_proxy_prefers_registry_over_env(monkeypatch):
    """★ 回归：注册表代理优先于环境变量代理。

    环境变量是 `60387`（死端口），注册表是 `7897`（真正在听的 Clash）。
    「跟随系统」必须返回 7897，否则又会隧道 502。
    """
    monkeypatch.setattr(
        "xydl.http._winreg_system_proxy",
        lambda: "http://127.0.0.1:7897",
    )
    # 让 getproxies() 返回被污染的环境变量值
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:60387", "http": "http://127.0.0.1:60387"},
    )
    assert system_proxy_url() == "http://127.0.0.1:7897"


def test_system_proxy_falls_back_to_env_when_no_registry(monkeypatch):
    """注册表没代理时，退回环境变量（比如纯命令行环境）。"""
    monkeypatch.setattr("xydl.http._winreg_system_proxy", lambda: "")
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7897"},
    )
    assert system_proxy_url() == "http://127.0.0.1:7897"


def test_system_proxy_empty_when_nothing_set(monkeypatch):
    monkeypatch.setattr("xydl.http._winreg_system_proxy", lambda: "")
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    assert system_proxy_url() == ""


def test_parse_proxy_spec_auto_uses_registry_not_env(monkeypatch):
    """auto 分支也要走注册表优先，不能拿到环境变量里的死端口。"""
    monkeypatch.setattr("xydl.http._winreg_system_proxy", lambda: "http://127.0.0.1:7897")
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:60387"},
    )
    assert parse_proxy_spec("auto") == "http://127.0.0.1:7897"
    assert parse_proxy_spec("") == "http://127.0.0.1:7897"
    assert parse_proxy_spec(None) == "http://127.0.0.1:7897"
