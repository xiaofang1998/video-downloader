"""控制台 HTTP 接口的集成测试 —— 起真实服务器打真实请求。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from xydl.channels.console import ConsoleChannel
from xydl.config import Config
from xydl.pipeline import Pipeline
from xydl.store import Store
from conftest import SAMPLE_BYTES, wait_for_terminal


class Api:
    """极简 HTTP 客户端，避免为测试引入 requests 依赖。"""

    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token

    def _open(self, path: str, method: str = "GET", payload=None):
        url = self.base + path
        if self.token:
            url += ("&" if "?" in url else "?") + f"token={self.token}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get(self, path: str):
        return self._open(path)

    def post(self, path: str, payload=None):
        return self._open(path, "POST", payload)

    def post_raw(self, path: str, body: bytes, content_type: str = "application/json"):
        url = self.base + path
        if self.token:
            url += ("&" if "?" in url else "?") + f"token={self.token}"
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": content_type},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get_json(self, path: str) -> dict:
        status, body = self.get(path)
        assert status == 200, body
        return json.loads(body)

    def post_json(self, path: str, payload=None) -> dict:
        status, body = self.post(path, payload)
        assert status == 200, body
        return json.loads(body)


@pytest.fixture
def console(config: Config, store: Store):
    config.set("server.port", 0)          # 让系统分配空闲端口
    config.set("server.token", "")
    pipeline = Pipeline(config, store)
    pipeline.start()
    channel = ConsoleChannel(pipeline, config)
    channel.start()
    yield Api(f"http://127.0.0.1:{channel.actual_port}"), pipeline, channel
    channel.stop()
    pipeline.stop()


def test_index_page_served(console):
    api, _, _ = console
    status, body = api.get("/")
    assert status == 200
    page = body.decode("utf-8")
    assert "视频下载工具" in page


def test_index_page_is_not_branded_for_xianyu(console):
    """★ 页面是当「视频下载工具」卖的，不能再出现闲鱼/接单那套措辞。

    这不会让程序崩，但买家一打开就看到「闲鱼接单控制台」—— 很尴尬。
    """
    api, _, _ = console
    page = api.get("/")[1].decode("utf-8")
    for word in ("接单控制台", "闲鱼", "买家", "复制回话"):
        assert word not in page, f"页面上还残留着「{word}」"


def test_state_endpoint_shape(console):
    api, _, _ = console
    data = api.get_json("/api/state")
    assert data["ok"] is True
    for key in ("stats", "engines", "search", "orders", "running"):
        assert key in data


def test_create_order_and_wait_for_done(console, store, sample_server):
    api, _, _ = console
    data = api.post_json("/api/orders", {
        "text": sample_server.url("sample.mp4"),
        "conversation_id": "buyer_42",
        "sender": "李四",
    })
    assert data["ok"] and data["created"]
    order_id = data["order"]["id"]

    final = wait_for_terminal(store, order_id)
    assert final.status == "done"
    assert Path(final.file_path).read_bytes() == SAMPLE_BYTES

    detail = api.get_json(f"/api/orders/{order_id}")
    assert detail["ok"]
    assert detail["file_exists"] is True
    assert "下载好啦" in detail["reply"]
    assert detail["events"]
    assert detail["sender"] == "李四"


def test_empty_text_is_rejected(console):
    api, _, _ = console
    status, body = api.post("/api/orders", {"text": "   "})
    assert status == 400
    assert "不能为空" in json.loads(body)["error"]


def test_malformed_json_is_rejected(console):
    api, _, _ = console
    status, body = api.post_raw("/api/orders", b"not json")
    assert status == 400
    assert "JSON" in json.loads(body)["error"]


def test_two_submissions_create_two_distinct_orders(console, sample_server):
    """两笔同样的消息应各自成单 —— 自动生成的 msg_id 不能撞号。"""
    api, _, _ = console
    payload = {"text": sample_server.url("sample.mp4"), "conversation_id": "same"}
    first = api.post_json("/api/orders", payload)
    second = api.post_json("/api/orders", payload)
    assert first["created"] and second["created"]
    assert first["order"]["id"] != second["order"]["id"]


def test_file_endpoint_returns_exact_bytes(console, store, sample_server):
    api, _, _ = console
    order_id = api.post_json("/api/orders",
                             {"text": sample_server.url("sample.mp4")})["order"]["id"]
    wait_for_terminal(store, order_id)
    status, body = api.get(f"/api/orders/{order_id}/file")
    assert status == 200
    assert body == SAMPLE_BYTES


def test_file_endpoint_404_before_done(console, store):
    api, _, _ = console
    order, _ = store.create_order("console:no-file", "console", "b", "在吗")
    status, _ = api.get(f"/api/orders/{order.id}/file")
    assert status == 404


def test_unknown_order_returns_404(console):
    api, _, _ = console
    status, _ = api.get("/api/orders/999999")
    assert status == 404


def test_retry_endpoint(console, store, sample_server):
    api, _, _ = console
    order_id = api.post_json("/api/orders",
                             {"text": sample_server.url("missing.mp4")})["order"]["id"]
    wait_for_terminal(store, order_id)
    data = api.post_json(f"/api/orders/{order_id}/retry")
    assert data["ok"] and data["applied"] is True


def test_replied_endpoint_marks_the_order(console, store):
    api, _, _ = console
    order, _ = store.create_order("console:replied", "console", "b", "在吗")
    api.post_json(f"/api/orders/{order.id}/replied")
    row = store.conn.execute("SELECT replied_at FROM orders WHERE id=?",
                             (order.id,)).fetchone()
    assert row["replied_at"] is not None


def test_order_list_filters(console, store, sample_server):
    api, _, _ = console
    api.post_json("/api/orders", {"text": sample_server.url("sample.mp4"),
                                  "conversation_id": "zzz"})
    data = api.get_json("/api/orders?q=zzz")
    assert data["ok"]
    assert len(data["orders"]) == 1


def test_unknown_route_returns_404(console):
    api, _, _ = console
    status, body = api.get("/api/nope")
    assert status == 404
    assert "没有这个路由" in json.loads(body)["error"]


# ── 上游桥接：channel / meta ──────────────────────────────────────────
def test_channel_and_meta_are_persisted(console, store):
    """上游靠 channel 决定「推不推回闲鱼」、靠 meta 决定「推给哪个账号」，

    所以这两个字段必须原样落库，不能只活在内存里。
    """
    api, _, _ = console
    data = api.post_json("/api/orders", {
        "text": "https://v.douyin.com/xxxx/",
        "conversation_id": "chat_1",
        "sender": "buyer_1",
        "channel": "xianyu",
        "meta": {"cookie_id": "acc1", "chat_id": "chat_1",
                 "upstream_order_id": "9001", "item_id": "777"},
    })
    assert data["ok"]
    assert data["order"]["channel"] == "xianyu"
    assert data["order"]["meta"]["cookie_id"] == "acc1"

    fresh = store.get(data["order"]["id"])
    assert fresh.channel == "xianyu"
    assert fresh.meta["upstream_order_id"] == "9001"
    assert fresh.meta["item_id"] == "777"


def test_default_channel_is_console(console):
    """人肉粘贴的单默认走 console —— 桥接渠道据此把它排除在外。"""
    api, _, _ = console
    data = api.post_json("/api/orders", {"text": "在吗"})
    assert data["order"]["channel"] == "console"
    assert data["order"]["meta"] == {}


def test_meta_must_be_object(console):
    api, _, _ = console
    status, body = api.post("/api/orders", {"text": "hi", "meta": "不是对象"})
    assert status == 400
    assert "meta" in json.loads(body)["error"]


def test_legacy_client_without_channel_still_works(console, store, sample_server):
    """老调用方（只传 text/conversation_id/sender）必须继续能用。"""
    api, _, _ = console
    data = api.post_json("/api/orders", {
        "text": sample_server.url("sample.mp4"), "conversation_id": "old_client",
    })
    assert data["ok"] and data["created"]
    assert wait_for_terminal(store, data["order"]["id"]).status == "done"


# ── 打开文件夹 ────────────────────────────────────────────────────────
def test_reveal_dir_opens_the_download_folder(console, monkeypatch):
    api, pipeline, _ = console
    opened: list = []
    monkeypatch.setattr("xydl.channels.console.open_in_file_manager",
                        lambda path, select=True: (opened.append(path), True)[1])
    data = api.post_json("/api/reveal-dir")
    assert data["ok"] and data["opened"] is True
    assert str(pipeline.download_dir) == data["path"]
    assert opened


def test_reveal_order_selects_the_file(console, store, sample_server, monkeypatch):
    api, _, _ = console
    opened: list = []
    monkeypatch.setattr("xydl.channels.console.open_in_file_manager",
                        lambda path, select=True: (opened.append(path), True)[1])

    order_id = api.post_json("/api/orders",
                             {"text": sample_server.url("sample.mp4")})["order"]["id"]
    final = wait_for_terminal(store, order_id)
    data = api.post_json(f"/api/orders/{order_id}/reveal")
    assert data["ok"]
    assert data["path"] == final.file_path
    assert Path(opened[0]).name == Path(final.file_path).name


def test_reveal_without_file_returns_404(console, store):
    api, _, _ = console
    order, _ = store.create_order("console:no-reveal", "console", "b", "在吗")
    status, _ = api.post(f"/api/orders/{order.id}/reveal")
    assert status == 404


def test_open_in_file_manager_never_raises(monkeypatch, tmp_path):
    """自动打开文件管理器只是便利功能，失败也不能让接口炸掉。"""
    from xydl.channels import console as console_mod

    def boom(*args, **kwargs):
        raise OSError("没有 explorer")

    monkeypatch.setattr(console_mod.subprocess, "Popen", boom)
    assert console_mod.open_in_file_manager(tmp_path) is False


# ── 鉴权 ──────────────────────────────────────────────────────────────
def test_token_protects_api(config: Config, store: Store):
    config.set("server.port", 0)
    config.set("server.token", "s3cret")
    pipeline = Pipeline(config, store)
    pipeline.start()
    channel = ConsoleChannel(pipeline, config)
    channel.start()
    try:
        base = f"http://127.0.0.1:{channel.actual_port}"

        # 无 token
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/api/state", timeout=5)
        assert exc.value.code == 401

        # 错误 token
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/api/state?token=wrong", timeout=5)
        assert exc.value.code == 401

        # 正确 token
        with urllib.request.urlopen(base + "/api/state?token=s3cret", timeout=5) as r:
            assert r.status == 200

        # 首页本身不需要 token（前端还要能读到 token 才能调接口）
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert r.status == 200
    finally:
        channel.stop()
        pipeline.stop()


def test_cdp_login_endpoint(console, monkeypatch):
    """★ 登录抖音/TikTok 的界面入口（TikHub 兜底）。"""
    import time
    called = {}

    def _fake_login(platform, url, wait_sec=0):
        called["platform"] = platform

    monkeypatch.setattr("xydl.cdp_fetch.login", _fake_login)
    api, _, _ = console
    data = api.post_json("/api/cdp-login", {"platform": "douyin"})
    assert data["ok"] is True
    time.sleep(0.6)  # 等后台线程跑起来
    assert called.get("platform") == "douyin"


def test_cdp_login_rejects_bad_platform(console):
    api, _, _ = console
    status, body = api.post("/api/cdp-login", {"platform": "bilibili"})
    assert status == 400
