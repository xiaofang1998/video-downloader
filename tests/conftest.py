"""pytest 公共夹具。"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from xydl.config import Config
from xydl.store import Store

#: 2 MiB 可校验内容（不是全零，能抓出截断/错位）
SAMPLE_BYTES = bytes(range(256)) * 8192


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass


class _QuietServer(socketserver.TCPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # noqa: ARG002
        pass


class LocalServer:
    """本地静态文件服务器。所有网络测试都打它，保证离线可跑、结果确定。"""

    def __init__(self, root: Path):
        handler = functools.partial(_QuietHandler, directory=str(root))
        self._httpd = _QuietServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self.thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, name: str) -> str:
        return f"{self.base}/{name}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def sample_server(tmp_path: Path):
    """提供 sample.mp4 / page.html / clip.webm 的本地服务器。"""
    root = tmp_path / "serve"
    root.mkdir()
    (root / "sample.mp4").write_bytes(SAMPLE_BYTES)
    (root / "clip.webm").write_bytes(SAMPLE_BYTES[: 512 * 1024])
    (root / "page.html").write_text(
        "<html><head><meta property='og:title' content='一个网页标题'></head>"
        "<body>不是视频</body></html>",
        encoding="utf-8",
    )
    server = LocalServer(root)
    yield server
    server.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """完全隔离到 tmp_path 的配置；强制直连（不走系统代理）。"""
    cfg = Config(
        {
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "download_dir": str(tmp_path / "downloads"),
                "inbox_dir": str(tmp_path / "inbox"),
                "log_dir": str(tmp_path / "logs"),
            },
            "net": {"proxy": "none", "timeout_sec": 5},
            "download": {
                "prefer_ytdlp": False,   # 测试一律走直链引擎：离线且确定
                "workers": 1,
                "task_timeout_sec": 30,
                "max_filesize_mb": 64,
            },
            "channels": {
                "console": {"enabled": False},
                "inbox": {"enabled": False},
            },
            "replies": {"progress_min_seconds": 0, "progress_max_count": 3},
        },
        path=tmp_path / "config.json",
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def store(config: Config) -> Store:
    st = Store(config.path_of("data_dir") / "test.sqlite3")
    yield st
    st.close()


def wait_for_terminal(store: Store, order_id: int, timeout: float = 30.0):
    """轮询等待订单进入终态。超时则抛错并附上当前状态，便于定位卡在哪一步。"""
    import time

    from xydl.models import OrderStatus

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = store.get(order_id)
        if last is not None and OrderStatus(last.status).is_terminal:
            return last
        time.sleep(0.05)
    status = f"{last.status}/{last.stage}" if last else "订单不存在"
    raise AssertionError(f"订单 {order_id} 在 {timeout}s 内没有结束（当前 {status}）")
