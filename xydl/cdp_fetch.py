"""CDP（Chrome DevTools Protocol）控制 Edge：登录态兜底解析抖音/TikTok。

抖音/TikTok 的 a_bogus 签名本地逆向不现实，TikHub 是主力；这里提供**登录态兜底**：
用 CDP 启动 Edge（独立 profile），让用户登录一次，之后复用这个 profile 访问视频页，
**拦截页面 JS 自动带签名发起的 detail 请求响应**，拿到无水印视频直链。

纯标准库（socket 手写 WebSocket），不引入依赖。
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

from .config import app_dir

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# 抖音 detail 接口的特征路径（页面 JS 自动带 a_bogus 签名请求它）
_DETAIL_MARKS = (
    "/aweme/v1/web/aweme/detail/",
    "/aweme/v1/aweme/detail/",
    "/aweme/v1/multi/aweme/detail/",
)


def _find_edge() -> str:
    for p in _EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    import shutil
    return shutil.which("msedge") or ""


def login_profile_dir(platform: str) -> Path:
    """登录态 profile 目录（工具自管，与用户日常浏览器隔离）。"""
    d = app_dir() / "browser-login" / platform
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pick_port() -> int:
    return 9400 + (os.getpid() % 300)


def _open_cdp(url: str, profile: Path, port: int):
    edge = _find_edge()
    if not edge:
        raise RuntimeError("没找到 Edge 浏览器")
    return subprocess.Popen(
        [edge, f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "--no-first-run", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _http_json(url: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return json.load(opener.open(url, timeout=5))


class _CDP:
    """最小 CDP 客户端：命令 + 事件流。"""

    def __init__(self, ws_url: str, timeout: float = 8.0):
        u = urlparse(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.send((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        self._id = 0

    def _send_frame(self, payload: str) -> None:
        data = payload.encode("utf-8")
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        self.sock.send(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv_frame(self) -> bytes:
        head = self.sock.recv(2)
        if len(head) < 2:
            return b""
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self.sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self.sock.recv(8))[0]
        masked = head[1] & 0x80
        mask = self.sock.recv(4) if masked else b""
        data = b""
        while len(data) < length:
            chunk = self.sock.recv(min(65536, length - len(data)))
            if not chunk:
                break
            data += chunk
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return data

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self._send_frame(json.dumps({"id": mid, "method": method,
                                     "params": params or {}}))
        while True:
            data = json.loads(self._recv_frame().decode("utf-8", "replace"))
            if data.get("id") == mid:
                return data

    def wait_event(self, match, timeout: float = 20.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.time()))
            try:
                raw = self._recv_frame()
            except socket.timeout:
                continue
            if not raw:
                continue
            data = json.loads(raw.decode("utf-8", "replace"))
            if data.get("method") and match(data):
                return data
        return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def fetch_douyin_video(url: str, profile: Path, wait_sec: float = 20.0) -> tuple[str, str]:
    """复用登录态 profile，访问抖音视频页，拦截 detail 响应拿视频直链。

    返回 ``(视频URL, 标题)``；失败返回 ``("", "")``。
    """
    port = _pick_port()
    proc = _open_cdp(url, profile, port)
    cdp = None
    try:
        # 等页面出现
        deadline = time.time() + wait_sec
        page = None
        while time.time() < deadline:
            time.sleep(1)
            try:
                pages = _http_json(f"http://127.0.0.1:{port}/json")
            except Exception:  # noqa: BLE001
                continue
            page = next((p for p in pages
                         if p.get("type") == "page" and "douyin" in p.get("url", "")), None)
            if page:
                break
        if not page:
            return "", ""

        cdp = _CDP(page["webSocketDebuggerUrl"])
        cdp.call("Network.enable")

        # 拦截 detail 响应，拿视频数据
        video_url = ""
        title = ""

        def on_detail(evt: dict) -> bool:
            u = (evt.get("params") or {}).get("response") or {}
            return any(m in u.get("url", "") for m in _DETAIL_MARKS)

        deadline = time.time() + wait_sec
        while time.time() < deadline:
            evt = cdp.wait_event(on_detail, timeout=min(5, deadline - time.time()))
            if not evt:
                continue
            req_id = (evt.get("params") or {}).get("requestId")
            try:
                body = cdp.call("Network.getResponseBody", {"requestId": req_id})
                data = json.loads(body.get("result", {}).get("body", "{}"))
            except Exception:  # noqa: BLE001
                continue
            aweme = (data.get("aweme_detail") or {}) or (data.get("aweme_details") or [{}])[0]
            title = str(aweme.get("desc") or title or "")
            video = aweme.get("video") or {}
            for key in ("play_addr", "play_addr_265", "play_addr_h264", "download_addr"):
                urls = (video.get(key) or {}).get("url_list") or []
                if urls and urls[0]:
                    video_url = urls[0]
                    break
            if video_url:
                break
        return video_url, title
    finally:
        if cdp:
            cdp.close()
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def login(platform: str, url: str, wait_sec: float = 60.0) -> Path:
    """启动 Edge 让用户登录，返回 profile 目录（登录态保存在里面）。"""
    profile = login_profile_dir(platform)
    port = _pick_port()
    proc = _open_cdp(url, profile, port)
    print(f"[cdp] 已打开 Edge，请在窗口里登录 {platform}（扫码），登录后关闭窗口")
    try:
        # 简单等待：用户登录 + 关窗口，或超时
        time.sleep(wait_sec)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    return profile
