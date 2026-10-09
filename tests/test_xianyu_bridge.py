"""闲鱼桥接渠道测试。

关键行为都是「错了会真的发错东西给买家」的那几件：
只回传来源渠道的订单、一笔订单只上传一次、上传挂了也得把话术发出去。
"""

from __future__ import annotations

import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from xydl.channels.xianyu_bridge import XianyuBridgeChannel
from xydl.config import Config
from xydl.models import Order, OrderStatus
from xydl.pipeline import Pipeline
from xydl.store import Store
from xydl.uploaders.base import UploadResult, Uploader

from conftest import wait_for_terminal


def _wait_for(probe, timeout: float = 20.0, interval: float = 0.05):
    """轮询等一个条件成立。

    订单落库置为 done 和「回话投递出去」是两步，前者先发生 —— 所以
    「看到 done 就去断言上游收到了」会偶发失败，必须等投递这一步。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(interval)
    return None


# ══════════════════════════════════════════════════════════════════════
#  假上游 + 假上传器
# ══════════════════════════════════════════════════════════════════════
class FakeUpstream:
    """假装是 super-butler 的 /api/xianyu/deliver 与 /health。"""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.received: list[dict] = []
        self.headers: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003
                pass

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path.rstrip("/") == "/health":
                    return self._send(200, {"status": "ok"})
                self._send(404, {"error": "nope"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                outer.headers.append(dict(self.headers))
                try:
                    outer.received.append(json.loads(raw.decode("utf-8")))
                except json.JSONDecodeError:
                    outer.received.append({"__raw": raw.decode("utf-8", "replace")})
                if outer.fail:
                    return self._send(500, {"ok": False})
                self._send(200, {"ok": True})

        # block_on_close=False 见 test_uploaders.py 里的说明：
        # 默认的 server_close() 会 join 还挂在 keep-alive 上的 handler 线程。
        server_cls = type("S", (socketserver.ThreadingTCPServer,), {
            "allow_reuse_address": True,
            "daemon_threads": True,
            "block_on_close": False,
            "handle_error": lambda *a: None,
        })
        self._httpd = server_cls(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, kwargs={'poll_interval': 0.05})
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class StubUploader(Uploader):
    """可控的上传器替身：记录调用次数，返回预设结果。"""

    name = "stub"
    label = "测试上传器"

    def __init__(self, config, result: UploadResult):
        super().__init__(config)
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def upload(self, path, remote_name="") -> UploadResult:
        self.calls.append((str(path), remote_name))
        return self.result


@pytest.fixture
def upstream():
    server = FakeUpstream()
    yield server
    server.close()


def _make_channel(tmp_path: Path, upstream_url: str, uploader: StubUploader,
                  **overrides) -> XianyuBridgeChannel:
    delivery = {
        "uploader": "none",
        "mode": "share_link",
        "forward_progress": False,
        "upload_fail_note": "（链接稍后补发）",
        "bridge": {
            "enabled": True,
            "upstream_url": upstream_url,
            "deliver_path": "/api/xianyu/deliver",
            "token": "shared-secret",
            "source_channels": ["xianyu"],
            "timeout_sec": 5,
            "retries": 0,
        },
    }
    delivery.update(overrides)
    config = Config(
        {
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "download_dir": str(tmp_path / "downloads"),
                "inbox_dir": str(tmp_path / "inbox"),
                "log_dir": str(tmp_path / "logs"),
            },
            "net": {"proxy": "none", "timeout_sec": 5},
            "delivery": delivery,
        },
        path=tmp_path / "config.json",
    )
    config.ensure_dirs()
    store = Store(config.path_of("data_dir") / "t.sqlite3")
    pipeline = Pipeline(config, store)
    channel = XianyuBridgeChannel(pipeline, config)
    channel.uploader = uploader
    return channel


def _order(**kw) -> Order:
    base = dict(
        id=1, channel="xianyu", conversation_id="chat_abc", sender="buyer_1",
        text="https://v.douyin.com/xxx/", status=OrderStatus.DONE.value,
        title="猫咪打呼噜", file_path="C:/dl/1/猫咪打呼噜.mp4",
        meta={"cookie_id": "acc1", "chat_id": "chat_abc", "buyer_id": "buyer_1",
              "upstream_order_id": "900001", "item_id": "777"},
    )
    base.update(kw)
    return Order(**base)


# ══════════════════════════════════════════════════════════════════════
#  路由：只回传该回传的
# ══════════════════════════════════════════════════════════════════════
def test_ignores_non_source_channel(tmp_path, upstream):
    """人肉粘贴的 console 单绝不能被推回闲鱼 —— 否则测试单会发给真买家。"""
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, upstream.base, stub)
    channel.deliver(_order(channel="console"), "你好", "C:/dl/a.mp4")
    assert upstream.received == []
    assert stub.calls == []


def test_ignores_order_without_conversation(tmp_path, upstream):
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, upstream.base, stub)
    channel.deliver(_order(conversation_id=""), "你好", "")
    assert upstream.received == []


def test_progress_not_forwarded_by_default(tmp_path, upstream):
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, upstream.base, stub)
    channel.deliver(_order(status=OrderStatus.DOWNLOADING.value, file_path=""),
                    "进度 50%", "")
    assert upstream.received == []


def test_progress_forwarded_when_enabled(tmp_path, upstream):
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, upstream.base, stub, forward_progress=True)
    channel.deliver(_order(status=OrderStatus.DOWNLOADING.value, file_path=""),
                    "进度 50%", "")
    assert len(upstream.received) == 1


def test_received_ack_is_forwarded(tmp_path, upstream):
    """接单确认要发 —— 不然买家以为没人理。"""
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, upstream.base, stub)
    channel.deliver(_order(status=OrderStatus.RECEIVED.value, file_path=""),
                    "链接收到啦～", "")
    assert upstream.received[0]["text"] == "链接收到啦～"


# ══════════════════════════════════════════════════════════════════════
#  交付：上传 + 拼链接 + 回传
# ══════════════════════════════════════════════════════════════════════
def test_done_uploads_and_appends_link(tmp_path, upstream):
    stub = StubUploader(None, UploadResult(
        ok=True, url="https://pan.baidu.com/s/1XX", pwd="ab12",
        remote_path="/apps/xianyu/1_猫咪打呼噜.mp4", fs_id="42"))
    channel = _make_channel(tmp_path, upstream.base, stub)

    channel.deliver(_order(), "下载好啦！", "C:/dl/1/猫咪打呼噜.mp4")

    assert len(stub.calls) == 1
    path, remote_name = stub.calls[0]
    assert path.endswith("猫咪打呼噜.mp4")
    assert remote_name.startswith("1_")          # 带订单号，防并发覆盖

    payload = upstream.received[0]
    assert payload["text"].startswith("下载好啦！")
    assert "https://pan.baidu.com/s/1XX" in payload["text"]
    assert "ab12" in payload["text"]
    assert payload["cookie_id"] == "acc1"        # 上游靠它找账号
    assert payload["chat_id"] == "chat_abc"      # 和 buyer_id，才知道发给谁
    assert payload["buyer_id"] == "buyer_1"
    assert payload["upstream_order_id"] == "900001"
    assert payload["media"]["url"] == "https://pan.baidu.com/s/1XX"
    assert payload["local_order_id"] == 1


def test_upload_happens_only_once(tmp_path, upstream):
    """流水线可能多次 emit 同一单，网盘不能被传两遍。"""
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, upstream.base, stub)
    order = _order()
    channel.deliver(order, "下载好啦", "C:/dl/a.mp4")
    channel.deliver(order, "下载好啦", "C:/dl/a.mp4")
    assert len(stub.calls) == 1
    assert len(upstream.received) == 2           # 话说两遍，文件只传一次


def test_upload_failure_still_sends_text(tmp_path, upstream):
    """上传挂了也必须把话术发出去，并且带一句安抚 —— 不能让买家干等。"""
    stub = StubUploader(None, UploadResult(ok=False, error="分享权限不足"))
    channel = _make_channel(tmp_path, upstream.base, stub)
    channel.deliver(_order(), "下载好啦！", "C:/dl/a.mp4")

    assert len(upstream.received) == 1
    text = upstream.received[0]["text"]
    assert "下载好啦！" in text
    assert "链接稍后补发" in text
    assert upstream.received[0]["media"] == {}


def test_upload_only_mode_skips_link(tmp_path, upstream):
    """分享权限没批下来时的过渡模式：文件传上去了但没链接，

    此时**不能**套用「上传失败」的安抚话术 —— 那会让运营以为白传了。
    """
    # 这正是 BaiduPanUploader 在「传成功但分享失败」时的返回：
    # ok=False，但 fs_id / remote_path 有值。
    stub = StubUploader(None, UploadResult(
        ok=False, fs_id="42", remote_path="/apps/xianyu/1_a.mp4",
        error="创建分享链接都失败了（可能是应用没有分享权限）"))
    channel = _make_channel(tmp_path, upstream.base, stub, mode="upload_only")
    channel.deliver(_order(), "下载好啦！", "C:/dl/a.mp4")
    assert upstream.received[0]["text"] == "下载好啦！"
    assert "链接稍后补发" not in upstream.received[0]["text"]


def test_upload_only_note_when_configured(tmp_path, upstream):
    stub = StubUploader(None, UploadResult(ok=False, fs_id="42",
                                           remote_path="/apps/xianyu/1_a.mp4",
                                           error="没分享权限"))
    channel = _make_channel(tmp_path, upstream.base, stub, mode="upload_only",
                            upload_only_note="（已存到网盘，稍后发你链接）")
    channel.deliver(_order(), "下载好啦！", "C:/dl/a.mp4")
    assert "已存到网盘" in upstream.received[0]["text"]


def test_upload_only_real_failure_still_warns(tmp_path, upstream):
    """upload_only 模式下如果**根本**没传上去，还是要提醒。"""
    stub = StubUploader(None, UploadResult(ok=False, error="网络错误：连不上"))
    channel = _make_channel(tmp_path, upstream.base, stub, mode="upload_only")
    channel.deliver(_order(), "下载好啦！", "C:/dl/a.mp4")
    assert "链接稍后补发" in upstream.received[0]["text"]


def test_need_manual_reason_is_forwarded(tmp_path, upstream):
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, upstream.base, stub)
    channel.deliver(_order(status=OrderStatus.NEED_MANUAL.value, file_path=""),
                    "亲，这个链接没解析出来", "")
    assert upstream.received[0]["text"] == "亲，这个链接没解析出来"


def test_token_header_is_sent(tmp_path, upstream):
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, upstream.base, stub)
    channel.deliver(_order(status=OrderStatus.RECEIVED.value, file_path=""), "hi", "")
    assert upstream.headers[0].get("X-Auth-Token") == "shared-secret"


def test_upstream_down_does_not_raise(tmp_path):
    """上游挂了不能把流水线带崩 —— 订单该走的流程还得走完。"""
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, "http://127.0.0.1:1", stub,
                            bridge={"enabled": True, "upstream_url": "http://127.0.0.1:1",
                                    "deliver_path": "/api/xianyu/deliver",
                                    "token": "", "source_channels": ["xianyu"],
                                    "timeout_sec": 2, "retries": 0})
    channel.deliver(_order(status=OrderStatus.RECEIVED.value, file_path=""), "hi", "")


def test_retry_eventually_succeeds(tmp_path):
    """前几次 500、后面成功，重试机制得吃得下。"""
    flaky = FakeUpstream(fail=True)
    try:
        stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
        channel = _make_channel(tmp_path, flaky.base, stub,
                                bridge={"enabled": True, "upstream_url": flaky.base,
                                        "deliver_path": "/api/xianyu/deliver",
                                        "token": "", "source_channels": ["xianyu"],
                                        "timeout_sec": 5, "retries": 1})
        channel.deliver(_order(status=OrderStatus.RECEIVED.value, file_path=""), "hi", "")
        assert len(flaky.received) == 2       # 第一次 + 重试一次
    finally:
        flaky.close()


def test_ping_reports_upstream_state(tmp_path, upstream):
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, upstream.base, stub)
    ok, message = channel.ping()
    assert ok and "/health" in message


def test_describe_mentions_uploader(tmp_path, upstream):
    stub = StubUploader(None, UploadResult(ok=True, url="U", pwd="P"))
    channel = _make_channel(tmp_path, upstream.base, stub)
    assert "闲鱼桥接" in channel.describe()
    assert "测试上传器" in channel.describe()


# ══════════════════════════════════════════════════════════════════════
#  端到端：HTTP 收单 → 真下载 → 上传 → 回传上游
# ══════════════════════════════════════════════════════════════════════
def test_full_chain_from_http_intake_to_upstream(tmp_path, sample_server, upstream):
    """把三个组件拼起来跑一遍。

    单测各自通过、拼起来跑不通是这类功能最典型的翻车方式：控制台收单、
    流水线下载、桥接回传，任何一个环节的字段对不上都会在这里暴露。
    """
    import json as _json
    import urllib.request

    from xydl.channels.base import ChannelManager
    from xydl.channels.console import ConsoleChannel

    config = Config(
        {
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "download_dir": str(tmp_path / "downloads"),
                "inbox_dir": str(tmp_path / "inbox"),
                "log_dir": str(tmp_path / "logs"),
            },
            "server": {"host": "127.0.0.1", "port": 0, "token": ""},
            "net": {"proxy": "none", "timeout_sec": 5},
            # 强制走直链引擎，测试才能离线且确定
            "download": {"prefer_ytdlp": False, "workers": 1, "task_timeout_sec": 30,
                         "max_filesize_mb": 64},
            "channels": {"console": {"enabled": True}, "inbox": {"enabled": False}},
            "replies": {"progress_min_seconds": 0},
            "delivery": {
                "uploader": "none",
                "mode": "share_link",
                "bridge": {
                    "enabled": True, "upstream_url": upstream.base,
                    "deliver_path": "/api/xianyu/deliver",
                    "token": "", "source_channels": ["xianyu"],
                    "timeout_sec": 5, "retries": 1,
                },
            },
        },
        path=tmp_path / "config.json",
    )
    config.ensure_dirs()

    store = Store(config.path_of("data_dir") / "t.sqlite3")
    pipeline = Pipeline(config, store)
    console = ConsoleChannel(pipeline, config)
    bridge = XianyuBridgeChannel(pipeline, config)
    # 把网盘换成替身：这条测试要验的是链路，不是百度接口
    bridge.uploader = StubUploader(None, UploadResult(
        ok=True, url="https://pan.baidu.com/s/1E2E", pwd="zz99",
        remote_path="/apps/xianyu/1_sample.mp4", fs_id="11"))

    manager = ChannelManager([console, bridge])
    pipeline.start()
    manager.start()
    try:
        assert not manager.errors, manager.errors
        payload = _json.dumps({
            "text": sample_server.url("sample.mp4"),
            "conversation_id": "chat_e2e",
            "sender": "buyer_e2e",
            "channel": "xianyu",
            "meta": {"cookie_id": "accE", "chat_id": "chat_e2e",
                     "buyer_id": "buyer_e2e", "upstream_order_id": "555"},
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{console.actual_port}/api/orders",
            data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            created = _json.loads(resp.read())

        order_id = created["order"]["id"]
        final = wait_for_terminal(store, order_id, timeout=40)
        assert final.status == "done", f"{final.status}/{final.stage} {final.error}"

        # 桥接必须至少回传过一次「完成 + 链接」。
        # 注意要等投递完成 —— 订单变 done 和回话发出去是两步。
        done_posts = _wait_for(lambda: [
            p for p in upstream.received if "pan.baidu.com/s/1E2E" in p["text"]
        ])
        assert done_posts, upstream.received
        assert "zz99" in done_posts[0]["text"]
        assert done_posts[0]["cookie_id"] == "accE"
        assert done_posts[0]["chat_id"] == "chat_e2e"
        assert done_posts[0]["upstream_order_id"] == "555"

        # 事件日志里应该有桥接留下的痕迹，方便运营排查
        messages = [e["message"] for e in store.events(order_id)]
        assert any("[桥接]" in m for m in messages)
    finally:
        manager.stop()
        pipeline.stop()
        store.close()
