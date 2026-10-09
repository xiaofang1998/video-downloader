"""本地网页控制台 —— 默认的「对接」方式：人机协同。

为什么是这样的形态：闲鱼没有开放 IM 接口，合规做法只能是「运营在中间」。
这个控制台把中间那一步压到两次点击：

    买家发链接  →  运营复制粘贴到控制台  →  系统自动跑完全流程
                →  点「复制回话」粘回闲鱼  →  点「下载文件」把成品发给买家

服务只监听 ``127.0.0.1``，并且支持 ``server.token`` 做一层简单鉴权。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .base import Channel
from ..config import ROOT, Config, bundle_dir
from ..models import Order
from ..utils import snippet

#: 用 bundle_dir() 而不是 ROOT：打包成桌面应用后源码不在磁盘上，
#: 页面是作为资源随包走的，得从 _MEIPASS 里找。
INDEX_FILE = bundle_dir() / "webui" / "index.html"


def open_in_file_manager(path: Path, select: bool = True) -> bool:
    """在系统的文件管理器里打开/定位一个路径。失败返回 False，不抛异常。

    这是本地控制台专用 —— 运营最常问的问题就是「文件下哪儿了」，
    一个按钮比让人抄路径强。
    """
    path = Path(path)
    try:
        if sys.platform == "win32":
            if select and path.is_file():
                # 注意必须是 '/select,<path>' 拼成一个参数，分开传会被空格坑
                subprocess.Popen(f'explorer /select,"{path}"')
            else:
                target = path if path.is_dir() else path.parent
                subprocess.Popen(["explorer", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if select else "", str(path)])
        else:
            target = path if path.is_dir() else path.parent
            subprocess.Popen(["xdg-open", str(target)])
        return True
    except OSError:
        return False

_FALLBACK_HTML = """<!doctype html><meta charset="utf-8">
<title>xydl 控制台</title>
<body style="font-family:system-ui;padding:2rem">
<h1>控制台页面缺失</h1>
<p>找不到 <code>webui/index.html</code>。接口仍然可用：</p>
<ul>
<li><code>POST /api/orders</code> {"text": "买家消息"}</li>
<li><code>GET /api/orders</code></li>
<li><code>GET /api/orders/&lt;id&gt;</code></li>
</ul>
</body>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "xydl-console/0.1"
    protocol_version = "HTTP/1.1"

    # ── 基础设施 ───────────────────────────────────────────────────
    @property
    def pipeline(self):
        return self.server.pipeline  # type: ignore[attr-defined]

    @property
    def app_config(self) -> Config:
        return self.server.app_config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 默认实现会往 stderr 刷每条请求，太吵；交给上层日志
        pass

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        token = str(self.app_config.get("server.token", "") or "")
        if not token:
            return True
        supplied = (
            self.headers.get("X-Auth-Token", "")
            or (query.get("token") or [""])[0]
        )
        return supplied == token

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    # ── 路由 ───────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        # 记一下「最近一次有人来访问」。桌面应用靠它判断窗口是不是已经关了
        # （页面每 2 秒轮询 /api/state，停了就说明窗口没了）。
        channel = getattr(self.server, "channel", None)
        if channel is not None:
            channel.last_request_at = time.time()

        if path != "/" and not self._authorized(query):
            self._error(401, "缺少或错误的 token（在 config.json 的 server.token 里设置）")
            return

        try:
            if path == "/":
                self._page_index()
                return
            if path == "/api/state":
                self._api_state()
                return
            if path == "/api/orders":
                if method == "POST":
                    self._api_create()
                else:
                    self._api_orders(query)
                return
            match = re.fullmatch(r"/api/orders/(\d+)", path)
            if match:
                self._api_order(int(match.group(1)))
                return
            match = re.fullmatch(r"/api/orders/(\d+)/(retry|cancel|replied|file|reveal)",
                                 path)
            if match:
                order_id, action = int(match.group(1)), match.group(2)
                if action == "file":
                    self._api_file(order_id)
                elif action == "reveal" and method == "POST":
                    self._api_reveal(order_id)
                elif method == "POST":
                    self._api_action(order_id, action)
                else:
                    self._error(405, f"{action} 需要 POST")
                return
            if path == "/api/reveal-dir" and method == "POST":
                self._api_reveal_dir()
                return
            if path == "/api/cdp-login" and method == "POST":
                self._api_cdp_login()
                return
            self._error(404, f"没有这个路由：{path}")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - 控制台绝不该 500 崩掉整进程
            self._error(500, f"{type(exc).__name__}: {exc}")

    # ── 处理函数 ───────────────────────────────────────────────────
    def _page_index(self) -> None:
        if INDEX_FILE.exists():
            try:
                body = INDEX_FILE.read_bytes()
            except OSError:
                body = _FALLBACK_HTML.encode("utf-8")
        else:
            body = _FALLBACK_HTML.encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def _api_state(self) -> None:
        payload = self.pipeline.snapshot()
        payload["ok"] = True
        payload["channels"] = self.server.channels_desc  # type: ignore[attr-defined]
        payload["orders"] = [o.to_dict() for o in self.pipeline.store.list_orders(limit=50)]
        self._json(200, payload)

    def _api_orders(self, query: dict[str, list[str]]) -> None:
        def one(key: str, default: str = "") -> str:
            return (query.get(key) or [default])[0]

        try:
            limit = max(1, min(int(one("limit", "50")), 500))
        except ValueError:
            limit = 50
        orders = self.pipeline.store.list_orders(
            status=one("status") or None,
            channel=one("channel") or None,
            search=one("q"),
            limit=limit,
        )
        self._json(200, {"ok": True, "orders": [o.to_dict() for o in orders]})

    def _api_create(self) -> None:
        data = self._read_json()
        text = str(data.get("text") or "").strip()
        if not text:
            raise ValueError("text 不能为空（把买家发来的消息整段贴进来即可）")
        # 上游桥接会把 channel 设成 xianyu、并带 meta（cookie_id 等）。
        # 这两个字段是给渠道层用的：桥接渠道靠它们决定「这条要不要推回闲鱼」，
        # 以及「推给哪个闲鱼账号」。人肉粘贴的订单不传，默认就是 console。
        meta = data.get("meta")
        if meta is not None and not isinstance(meta, dict):
            raise ValueError("meta 必须是 JSON 对象")
        order, created = self.pipeline.submit_text(
            text=text,
            conversation_id=str(data.get("conversation_id") or "").strip() or "console",
            channel=str(data.get("channel") or "console").strip() or "console",
            sender=str(data.get("sender") or "").strip(),
            meta=meta or {},
        )
        self._json(200, {"ok": True, "created": created, "order": order.to_dict()})

    def _api_order(self, order_id: int) -> None:
        order = self.pipeline.store.get(order_id)
        if order is None:
            self._error(404, f"订单 {order_id} 不存在")
            return
        payload = order.to_dict()
        payload["ok"] = True
        payload["reply"] = self.pipeline.replies.reply_for(order)
        payload["events"] = self.pipeline.store.events(order_id)
        payload["file_exists"] = bool(order.file_path) and Path(order.file_path).exists()
        self._json(200, payload)

    def _api_action(self, order_id: int, action: str) -> None:
        if action == "retry":
            done = self.pipeline.retry(order_id)
        elif action == "cancel":
            done = self.pipeline.cancel(order_id)
        else:  # replied
            if self.pipeline.store.get(order_id) is None:
                done = False
            else:
                self.pipeline.store.mark_replied(order_id)
                done = True
        self._json(200, {"ok": True, "applied": done})

    def _api_file(self, order_id: int) -> None:
        order = self.pipeline.store.get(order_id)
        if order is None or not order.file_path:
            self._error(404, "这笔订单还没有产出文件")
            return
        path = Path(order.file_path)
        if not path.exists() or not path.is_file():
            self._error(404, f"文件已不存在：{path.name}")
            return
        if not self._within_download_dir(path):
            self._error(403, "出于安全考虑，只能下载下载目录里的文件")
            return
        body = path.read_bytes()
        quoted = urllib.parse.quote(path.name)
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name) or "video"
        self._send(
            200, body, "application/octet-stream",
            {"Content-Disposition":
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"},
        )

    def _within_download_dir(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.pipeline.download_dir.resolve())
            return True
        except ValueError:
            return False

    def _api_reveal(self, order_id: int) -> None:
        """在文件管理器里定位产物 —— 省得用户满硬盘找文件。"""
        order = self.pipeline.store.get(order_id)
        if order is None or not order.file_path:
            self._error(404, "这笔订单还没有产出文件")
            return
        path = Path(order.file_path)
        if not self._within_download_dir(path):
            self._error(403, "只能打开下载目录里的文件")
            return
        self._json(200, {"ok": True, "path": str(path),
                         "opened": open_in_file_manager(path)})

    def _api_reveal_dir(self) -> None:
        directory = Path(self.pipeline.download_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self._json(200, {"ok": True, "path": str(directory),
                         "opened": open_in_file_manager(directory)})

    def _api_cdp_login(self) -> None:
        """触发登录抖音/TikTok（登录态存进独立 profile，作为 TikHub 兜底）。"""
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        platform = str(body.get("platform") or "douyin").lower()
        if platform not in ("douyin", "tiktok"):
            self._error(400, "platform 只能是 douyin / tiktok")
            return

        def _run() -> None:
            from xydl.cdp_fetch import login
            login(platform,
                  "https://www.douyin.com/" if platform == "douyin"
                  else "https://www.tiktok.com/",
                  wait_sec=180)

        threading.Thread(target=_run, daemon=True).start()
        self._json(200, {"ok": True,
                         "message": f"已打开浏览器窗口，请在窗口里扫码登录 {platform}，"
                                    "登录后关闭窗口即可"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 的语义和 Unix 不同：它允许**绑定一个已经在监听的
    # 端口**。结果是第二次启动会"静默成功"，两个进程抢同一个端口，请求随机落到
    # 其中一个，排查起来极其迷惑。显式关掉，让重复启动明确失败。
    allow_reuse_address = False


class PortInUseError(RuntimeError):
    """控制台端口被占用。"""


class ConsoleChannel(Channel):
    """把本地 HTTP 控制台跑起来，并接收流水线的回话。"""

    name = "console"
    label = "本地网页控制台"

    def __init__(self, pipeline, config: Config):
        super().__init__(pipeline, config)
        self.host = str(config.get("server.host", "127.0.0.1") or "127.0.0.1")
        # 注意不能用 `x or 8765`：端口 0 是合法值（让系统分配空闲端口），
        # 但 0 是 falsy，会被 `or` 悄悄换成 8765。
        raw_port = config.get("server.port", 8765)
        self.port = 8765 if raw_port is None else int(raw_port)
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        #: 最近的回话，供控制台在没有轮询到 outbox 时兜底展示
        self.recent: list[dict] = []
        #: 最近一次收到请求的时间戳。桌面应用用它判断窗口是否还开着
        #: （页面每 2 秒轮询一次 /api/state）。0 表示还从没人访问过。
        self.last_request_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def actual_port(self) -> int:
        """实际监听的端口。配置成 0 时由系统分配，跑起来才知道是几。"""
        if self._server is not None:
            return int(self._server.server_address[1])
        return self.port

    @property
    def url(self) -> str:
        token = str(self.config.get("server.token", "") or "")
        suffix = f"/?token={urllib.parse.quote(token)}" if token else "/"
        return f"http://{self.host}:{self.actual_port}{suffix}"

    def start(self) -> None:
        super().start()   # 注册 deliver
        try:
            server = _Server((self.host, self.port), _Handler)
        except OSError as exc:
            raise PortInUseError(
                f"端口 {self.port} 起不来（{exc}）。"
                f"通常是已经有一个实例在跑了；"
                f"关掉旧窗口，或改 config.json 里的 server.port"
            ) from exc
        server.pipeline = self.pipeline          # type: ignore[attr-defined]
        server.app_config = self.config          # type: ignore[attr-defined]
        server.channels_desc = self.label        # type: ignore[attr-defined]
        server.channel = self                    # type: ignore[attr-defined]
        self._server = server
        # poll_interval 默认 0.5s，意味着 shutdown() 最多要等半秒才返回。
        # 服务本身是长驻的，但这半秒在「测试里每个用例都起一次服务」时就是几十秒。
        self._thread = threading.Thread(
            target=server.serve_forever, name="xydl-console", daemon=True,
            kwargs={"poll_interval": 0.05},
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def set_channels_desc(self, text: str) -> None:
        """由编排层塞进「当前启用了哪些渠道」的汇总。

        渠道之间互相不认识，``channels_desc`` 原本被写死成本渠道自己的 label，
        于是首页只显示「本地网页控制台」—— 开了桥接也看不见，很容易让人以为
        桥接没生效。
        """
        if self._server is not None:
            self._server.channels_desc = text  # type: ignore[attr-defined]

    def deliver(self, order: Order, text: str, file_path: str) -> None:
        with self._lock:
            self.recent.append({
                "order_id": order.id,
                "conversation_id": order.conversation_id,
                "text": text,
                "file_path": file_path,
            })
            del self.recent[:-200]

    def describe(self) -> str:
        return f"{self.label} {self.url}"
