"""通过 CDP（Chrome DevTools Protocol）从 Edge 拿明文 cookie。

为什么需要它：Edge 127+ 把 cookie 用 App-Bound Encryption（v20）加密，
第三方进程（包括 yt-dlp）**解不开**（IElevator COM 接口严格校验调用者必须是
浏览器进程，绕过只能 DLL 注入）。但 CDP 是浏览器自己吐出的**明文** cookie，
完全绕开加密。这里只用到 Python 标准库（socket 手写 WebSocket），不引入依赖。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _find_edge() -> str:
    for p in _EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    import shutil
    found = shutil.which("msedge")
    return found or ""


def _ws_request(ws_url: str, cmd: dict, timeout: float = 8.0) -> dict:
    """最小 WebSocket 客户端：发一条 CDP 命令，收一条 JSON 响应。"""
    u = urlparse(ws_url)
    s = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        s.send((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(4096)
        payload = json.dumps(cmd).encode("utf-8")
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        s.send(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        data = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            data += c
            if data.rstrip().endswith(b"}"):
                break
    finally:
        s.close()
    a, b = data.find(b"{"), data.rfind(b"}") + 1
    if a < 0 or b <= a:
        return {}
    return json.loads(data[a:b].decode("utf-8", "replace"))


def _get_cookies(port: int) -> list[dict]:
    """从 CDP 端口拿所有 cookie。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    pages = json.load(opener.open(f"http://127.0.0.1:{port}/json", timeout=5))
    for p in pages:
        if p.get("type") == "page" and "douyin" in p.get("url", ""):
            resp = _ws_request(p["webSocketDebuggerUrl"],
                               {"id": 1, "method": "Network.getAllCookies"})
            return (resp.get("result") or {}).get("cookies", [])
    return []


def douyin_guest_cookies(url: str, wait_sec: int = 15) -> dict[str, str]:
    """启动一个 Edge 窗口访问抖音，通过 CDP 拿明文 cookie（含 UIFID）。

    返回 ``{name: value}`` 的 cookie 字典；拿不到返回空 dict。

    会弹一个 Edge 窗口（抖音风控靠浏览器环境，headless 会被识别），约十几秒
    后自动关闭。这是绕开 Edge v20 加密、且买家只有 Edge 时唯一可行的路径。
    """
    edge = _find_edge()
    if not edge:
        return {}
    import tempfile
    profile = os.path.join(tempfile.gettempdir(), f"xydl_edge_{os.getpid()}")
    port = 9300 + (os.getpid() % 400)  # 给个独立端口，避免冲突

    proc = subprocess.Popen(
        [edge, f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "--no-first-run", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                cookies = _get_cookies(port)
            except Exception:  # noqa: BLE001 - Edge 还没就绪就继续等
                continue
            if not cookies:
                continue
            names = {c.get("name") for c in cookies}
            # 等到 s_v_web_id + ttwid 齐了就算拿到（UIFID 可能晚一点）
            if "s_v_web_id" in names and "ttwid" in names:
                return {c.get("name"): c.get("value") for c in cookies
                        if c.get("name") and c.get("value") is not None}
        return {}
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        import shutil
        shutil.rmtree(profile, ignore_errors=True)


def cookies_to_netscape(cookies: dict[str, str], domain: str = ".douyin.com") -> str:
    """把 cookie 字典转成 Netscape cookies.txt 文本。"""
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in cookies.items():
        lines.append(f"{domain}\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
    return "\n".join(lines) + "\n"
