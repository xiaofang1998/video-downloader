"""视频号本地抓包（选择性 MITM 代理）的端到端验证。

不依赖微信：本地起 HTTPS 服务器扮演微信接口，用**真实域名**
``channels.weixin.qq.com`` 走一遍 CONNECT → 中间人 → 抓取。

两个刻意的选择：

* **不用 urllib 当客户端**。Python 的 ``urllib`` 会 ``proxy_bypass`` 掉
  ``127.0.0.1``（Windows 注册表默认绕过 localhost），请求根本不进代理 ——
  我第一次就栽在这儿：测试红了，代理其实是对的。代理只能自己写报文来测。
* **用真实域名 + upstream_map**，而不是把 ``127.0.0.1`` 加进白名单。这样
  "白名单判定 + 按域名签叶证书 + SNI 正确"这几件事才是真被验证过的。
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from xydl import wxproxy
from xydl.mitm import CertAuthority, find_openssl

TARGET_HOST = "channels.weixin.qq.com"
VIDEO_URL = "https://finder.video.qq.com/251/20302/stodownload?encfilekey=abc&token=t"
COVER_URL = "https://finder.video.qq.com/251/20350/stodownload?encfilekey=abc&picformat=200"

FEED_JSON = {
    "errCode": 0,
    "data": {
        "objectDesc": {
            "description": "出狱后，我举世无双-第1集",
            "media": [{"url": VIDEO_URL, "decodeKey": "2136343393",
                       "coverUrl": COVER_URL, "fileSize": 14088528}],
        }
    },
}


# ══════════════════════════════════════════════════════════════════════
#  纯逻辑（不需要证书/网络）
# ══════════════════════════════════════════════════════════════════════
def test_extract_media_finds_url_and_key():
    hits = wxproxy.extract_media(FEED_JSON)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.url == VIDEO_URL
    assert hit.decode_key == "2136343393", "decode_key 在父层，必须继承下来"
    assert hit.title == "出狱后，我举世无双-第1集"


def test_extract_media_ignores_cover():
    """封面走同一个 stodownload 接口，绝不能当视频。"""
    assert wxproxy.extract_media({"coverUrl": COVER_URL, "decodeKey": "1"}) == []


def test_extract_media_handles_client_api_shape():
    """客户端接口的结构（object_desc.media[0].decode_key 下划线版）也要认。"""
    payload = {"data": {"object_desc": {"media": [
        {"url": VIDEO_URL, "decode_key": 987654321, "title": "某剧"}]}}}
    hits = wxproxy.extract_media(payload)
    assert hits and hits[0].decode_key == "987654321"


def test_extract_media_dedupes_preferring_key():
    payload = {"a": {"url": VIDEO_URL}, "b": {"url": VIDEO_URL, "decodeKey": "7"}}
    hits = wxproxy.extract_media(payload)
    assert len(hits) == 1 and hits[0].decode_key == "7"


def test_store_add_and_query(tmp_path):
    import time

    store = wxproxy.CaptureStore(path=tmp_path / "cap.json")
    assert store.add({"url": VIDEO_URL, "decode_key": "", "source": "cdn",
                      "ts": time.time()})
    assert store.latest(need_key=False)["url"] == VIDEO_URL
    assert store.latest() is None, "没有密钥的条目不能拿来下载"

    # 后到的 decode_key 要能补到已有条目上（媒体请求先来、API 响应后到是常态）
    assert store.add({"url": VIDEO_URL, "decode_key": "42", "source": "api",
                      "ts": time.time()})
    assert store.latest()["decode_key"] == "42"

    again = wxproxy.CaptureStore(path=tmp_path / "cap.json")
    assert again.latest()["decode_key"] == "42", "必须能跨进程/重启复用"


def test_store_expired_key_is_not_reused(tmp_path):
    """decode_key 与直链必须成对：过期的 key 宁可不要，也不能拿去解密。"""
    store = wxproxy.CaptureStore(path=None)
    store.add({"url": VIDEO_URL, "decode_key": "1", "ts": 1.0})
    assert store.latest(max_age=60) is None


def test_store_rejects_non_video_url():
    store = wxproxy.CaptureStore(path=None)
    assert not store.add({"url": COVER_URL, "decode_key": "1", "ts": 1e12})


def test_store_find_by_title():
    import time

    store = wxproxy.CaptureStore(path=None)
    now = time.time()
    store.add({"url": VIDEO_URL, "decode_key": "1", "title": "某某短剧-第1集", "ts": now})
    store.add({"url": VIDEO_URL + "&i=2", "decode_key": "2", "title": "某某短剧-第2集",
               "ts": now})
    assert store.find("某某短剧-第2集")["decode_key"] == "2"
    assert store.find("不存在的剧") is None


# ══════════════════════════════════════════════════════════════════════
#  证书
# ══════════════════════════════════════════════════════════════════════
def test_ca_generates_and_signs(tmp_path):
    if not find_openssl():
        pytest.skip("没有 openssl（签证书需要）")
    ca = CertAuthority(tmp_path / "ca")
    ok, message = ca.ensure()
    assert ok, message
    cert, key = ca.leaf_for(TARGET_HOST)
    assert cert and key and Path(cert).exists()
    assert ca.leaf_for(TARGET_HOST) == (cert, key), "叶证书必须走缓存"


def test_ca_leaf_supports_ip(tmp_path):
    if not find_openssl():
        pytest.skip("没有 openssl")
    ca = CertAuthority(tmp_path / "ca")
    ca.ensure()
    assert ca.leaf_for("127.0.0.1")[0], "IP 也要能签"


# ══════════════════════════════════════════════════════════════════════
#  端到端：请求真的穿过代理
# ══════════════════════════════════════════════════════════════════════
class _FeedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "json"

    def log_message(self, *args):
        pass

    def do_GET(self):                       # noqa: N802
        if _FeedHandler.mode == "json":
            body, ctype = json.dumps(FEED_JSON).encode(), "application/json"
        elif _FeedHandler.mode == "spec":
            body, ctype = json.dumps(SPEC_PAYLOAD).encode(), "application/json"
        else:
            body, ctype = b"plain-passthrough-body", "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve_plain() -> tuple[ThreadingHTTPServer, int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, int(httpd.server_address[1])


def _serve_tls(ca: CertAuthority) -> tuple[ThreadingHTTPServer, int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(ca.ca_crt), str(ca.ca_key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, int(httpd.server_address[1])


# ── 自己写报文的客户端：urllib 会绕过 localhost 代理，不能用 ──────────
def _read_all(sock: socket.socket) -> bytes:
    data = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data


def _split_response(raw: bytes) -> tuple[int, str, bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if len(lines[0].split(" ")) > 1 else 0
    return status, head.decode("latin-1"), body


def _proxy_get(proxy_port: int, host: str, port: int, path: str, ca: CertAuthority,
               *, hostname: str | None = None, tunnel: bool = True) -> bytes:
    """通过代理发一条 GET，返回原始响应字节。"""
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    if tunnel:
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\n"
                     f"Host: {host}:{port}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("代理没有回应 CONNECT")
            head += chunk
        if b" 200" not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"CONNECT 被拒：{head[:80]!r}")
        ctx = ssl.create_default_context(cafile=str(ca.ca_crt))
        sock = ctx.wrap_socket(sock, server_hostname=hostname or host)
        target = path
    else:
        target = f"http://{host}:{port}{path}"
    sock.sendall(f"GET {target} HTTP/1.1\r\nHost: {host}\r\n"
                 "Connection: close\r\n\r\n".encode())
    raw = _read_all(sock)
    sock.close()
    return raw


def _proxy_post(proxy_port: int, host: str, port: int, path: str, ca: CertAuthority,
                body: bytes) -> bytes:
    """通过代理（MITM 隧道）发一条 POST，返回原始响应字节。"""
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\n"
                 f"Host: {host}:{port}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("代理没有回应 CONNECT")
        head += chunk
    ctx = ssl.create_default_context(cafile=str(ca.ca_crt))
    sock = ctx.wrap_socket(sock, server_hostname=host)
    sock.sendall(f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
                 f"Content-Length: {len(body)}\r\n"
                 "Content-Type: text/plain\r\nConnection: close\r\n\r\n".encode() + body)
    raw = _read_all(sock)
    sock.close()
    return raw


@pytest.fixture()
def mitm_env(tmp_path):
    if not find_openssl():
        pytest.skip("没有 openssl（签证书需要）")
    ca = CertAuthority(tmp_path / "ca")
    ok, message = ca.ensure()
    assert ok, message
    tls_httpd, tls_port = _serve_tls(ca)
    store = wxproxy.CaptureStore(path=tmp_path / "cap.json")
    logs: list[str] = []
    proxy = wxproxy.ChannelsProxy(
        store, port=0, ca=ca,
        upstream_map={TARGET_HOST: ("127.0.0.1", tls_port)},
        verify_upstream=False,             # 测试上游是自签的
        log=logs.append)
    proxy.start()
    yield store, proxy, tls_port, ca, logs
    proxy.stop()
    tls_httpd.shutdown()
    tls_httpd.server_close()


def test_proxy_captures_url_and_decode_key(mitm_env):
    """核心用例：HTTPS 请求穿过代理后，媒体地址 + decode_key 必须被截下来。"""
    store, proxy, _tls_port, ca, logs = mitm_env
    _FeedHandler.mode = "json"

    raw = _proxy_get(proxy.port, TARGET_HOST, 443,
                     "/cgi-bin/mmfinderassistant-bin/feed", ca)
    status, _head, body = _split_response(raw)

    assert status == 200, f"响应异常：{raw[:200]!r}"
    assert json.loads(body)["errCode"] == 0, "代理不能把响应体弄坏"
    found = store.with_key()
    assert found, f"没抓到 url+key；日志：{logs}"
    assert found[0]["url"] == VIDEO_URL
    assert found[0]["decode_key"] == "2136343393"
    assert proxy.stats["mitm"] >= 1


def test_proxy_tunnels_non_target_hosts(mitm_env):
    """非目标域名必须**字节级原样透传**：不解密、不改内容、不记录。

    这条很重要：白名单外的流量如果被我们碰坏了，用户看到的现象是"某个网站打不开"，
    而不会怀疑到下载工具头上。
    """
    store, proxy, _tls_port, ca, logs = mitm_env
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(5)
    echo_port = echo.getsockname()[1]
    proxy.upstream_map["example.com"] = ("127.0.0.1", echo_port)

    def echo_once() -> None:
        conn, _ = echo.accept()
        data = conn.recv(1024)
        conn.sendall(b"echo:" + data)
        conn.close()

    threading.Thread(target=echo_once, daemon=True).start()
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.sendall(b"CONNECT example.com:8443 HTTP/1.1\r\n"
                 b"Host: example.com:8443\r\n\r\n")
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    assert b" 200" in head.split(b"\r\n")[0], "隧道必须建立"
    sock.sendall(b"ping-through-tunnel")
    got = sock.recv(1024)
    sock.close()
    echo.close()

    assert got == b"echo:ping-through-tunnel", "隧道必须原样搬运字节"
    assert store.entries() == [], "非目标流量不该被记录"
    assert proxy.stats["tunnel"] >= 1
    assert proxy.stats["mitm"] == 0, "白名单外的域名绝不能被中间人"


def test_proxy_captures_plain_http(mitm_env):
    """明文 HTTP 路径也要能抓（保底：有的接口不走 CONNECT）。"""
    store, proxy, _tls_port, ca, logs = mitm_env
    _FeedHandler.mode = "json"
    plain_httpd, plain_port = _serve_plain()
    proxy.upstream_map[TARGET_HOST] = ("127.0.0.1", plain_port)
    try:
        raw = _proxy_get(proxy.port, TARGET_HOST, plain_port,
                         "/cgi-bin/mmfinderassistant-bin/feed", ca, tunnel=False)
        status, _head, body = _split_response(raw)
        assert status == 200 and json.loads(body)["errCode"] == 0
        assert store.with_key(), f"明文路径没抓到；日志：{logs}"
    finally:
        plain_httpd.shutdown()
        plain_httpd.server_close()


# ── 最小侵入版注入（上一版把用户微信页面搞坏，这几条是硬约束）──────────
def test_inject_js_is_read_only_and_inert():
    """注入代码必须是**纯读**、非视频号页面惰性。

    两次把用户微信页面弄坏的教训都在这里：
      * 用 `Object.defineProperty(self,'onmessage',…)` 改消息入口 → 解密 worker 崩
      * 用 `Object.defineProperty(MessagePort.prototype,…)` 改全局原型 → 更糟
    所以"改行为"一律禁止，只允许读 + 同源回报。
    """
    js = wxproxy._INJECT_JS
    assert "defineProperty" not in js, "禁止改运行时属性的行为（弄坏过用户页面）"
    assert "MessagePort" not in js, "禁止碰 MessagePort（弄坏过用户页面）"
    assert "window.fetch" not in js and "XMLHttpRequest" not in js, "禁止 hook 全局"
    assert "onmessage=" not in js and "onmessage =" not in js, "禁止包消息入口"
    assert "127.0.0.1" not in js, "回报必须同源"
    assert wxproxy.REPORT_PATH in js
    # 只读目标：模块对象里的已生成密钥流
    assert "decryptor_array" in js
    assert "typeof p!=='undefined'" in js, "靠闭包探测同文件作用域里的顶层变量"


def test_inject_js_is_valid_javascript():
    """注入代码必须是**语法合法**的 JS —— 用 node 校一遍。

    踩过的坑：探测表里混进了保留字 `default`，生成的
    `typeof default!=='undefined'` 是 SyntaxError，整段注入代码解析失败、
    **静默不执行**（现象是"注入成功但零回报"，白抓好几轮）。
    这类错误必须由测试挡住，不能靠肉眼看。
    """
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path as _Path

    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过语法校验")
    with tempfile.TemporaryDirectory() as tmp:
        path = _Path(tmp) / "inject.js"
        path.write_text(wxproxy._INJECT_JS, encoding="utf-8")
        proc = subprocess.run([node, "--check", str(path)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
    assert proc.returncode == 0, f"注入代码语法错误：{proc.stderr[:400]}"


def test_inject_names_has_no_reserved_words():
    """探测表里不能有 JS 保留字（`typeof <保留字>` 直接是语法错误）。"""
    reserved = {"default", "class", "function", "return", "var", "let", "const",
                "new", "delete", "typeof", "void", "in", "of", "do", "if", "else",
                "case", "switch", "throw", "try", "catch", "finally", "while",
                "for", "break", "continue", "this", "super", "import", "export"}
    bad = reserved.intersection(wxproxy._INJECT_NAMES)
    assert not bad, f"探测表含保留字：{sorted(bad)}"


def test_inject_js_goes_inside_iife():
    """文件是 IIFE 时，注入代码必须落在**闭包内部**，否则读不到里面的变量。

    实测：`worker_release.js` = `(()=>{ … })();`，里面所有变量（含我们要读的
    `p.decryptor_array`）都在闭包里。追加到文件末尾 → `typeof p==='undefined'`
    → 探测全落空 → 零回报（连白抓好几轮）。这里用最小样本钉住行为。
    """
    wrapped = b"(()=>{ var p={onmessage:1,decryptor_array:new Uint8Array(4)}; })();"
    out = wxproxy._inject_js(wrapped).decode()
    assert out.rstrip().endswith("})();"), "外层 IIFE 的收尾必须还在最后"
    # 注入代码要在**最后一个** })();（即外层闭包的收尾）之前
    assert out.index("__xydlRead") < out.rindex("})();"), "注入代码要在闭包内部"

    # 没有外层闭包（emscripten 那种顶层 var）→ 追加在末尾即可
    plain = b"var Module={}; run();\n"
    out2 = wxproxy._inject_js(plain).decode()
    assert out2.startswith("var Module={}; run();")
    assert "__xydlRead" in out2


def test_should_inject_only_decrypt_core():
    """爆炸半径缩到最小：只注解密核心那两个文件。

    之前撒到 res.wx.qq.com 上 ~60 个**全站共享** bundle，风险过大。
    """
    proxy = wxproxy.ChannelsProxy(wxproxy.CaptureStore(path=None), port=0)
    core = "/t/wx_fed/cdn_libs/res/decrypt-video-core/1.3.2-preload.8/worker_release.js"
    assert proxy.should_inject("res.wx.qq.com", core,
                               {"content-type": "application/javascript"}) is True
    assert proxy.should_inject(
        "res.wx.qq.com", "/t/wx_fed/finder/web/web-finder/res/js/feed.publishX.js",
        {"content-type": "application/javascript"}) is False, "页面 bundle 不许碰"
    assert proxy.should_inject("channels.weixin.qq.com", "/web/pages/home",
                               {"content-type": "text/html"}) is False
    assert proxy.should_inject("finder.video.qq.com", "/x.js",
                               {"content-type": "application/javascript"}) is False


def test_injection_is_off_by_default(monkeypatch):
    """注入默认关闭：它可能影响用户的微信页面，必须显式开启。"""
    monkeypatch.delenv("XYDL_CAPTURE_INJECT", raising=False)
    assert wxproxy.ChannelsProxy(wxproxy.CaptureStore(path=None), port=0).inject is False
    monkeypatch.setenv("XYDL_CAPTURE_INJECT", "1")
    assert wxproxy.ChannelsProxy(wxproxy.CaptureStore(path=None), port=0).inject is True


def test_source_never_strips_csp():
    """源码里不许再出现摘 CSP 的写法（上一版就是它 + HTML 注入把页面弄坏的）。"""
    src = (Path(wxproxy.__file__)).read_text(encoding="utf-8")
    assert "content-security-policy" not in src.lower()


def test_report_endpoint_captures_without_upstream(mitm_env):
    """注入回报走**同源**路径，代理在转发前拦下并回 200（不碰上游服务器）。"""
    store, _proxy, _tls_port, ca, logs = mitm_env
    body = json.dumps({
        "tag": "media",
        "url": "https://finder.video.qq.com/251/20302/stodownload?encfilekey=E1",
        "decode_key": "2136343393",
        "spec": [{"url": "https://finder.video.qq.com/251/20302/stodownload?s=1080",
                  "height": 1080, "fileFormat": "1080P"}],
    }).encode()
    proxy = _proxy
    raw = _proxy_post(proxy.port, "channels.weixin.qq.com", 443,
                      wxproxy.REPORT_PATH, ca, body)
    status, _head, _payload = _split_response(raw)
    assert status == 200, raw[:160]
    # payload 里本来就含"媒体直链"+"spec 档位"两条，都要落库且都带 decode_key
    with_key = store.with_key()
    assert len(with_key) >= 1, f"decode_key 没落库：{logs}"
    assert all(e.get("decode_key") == "2136343393" for e in with_key)
    assert any(str(e.get("url", "")).startswith(
        "https://finder.video.qq.com/251/20302/stodownload?encfilekey=E1")
        for e in with_key), "媒体直链没落库"
    assert [e.get("height") for e in store.entries()
            if e.get("source") == "spec"] == ["1080"]


# ── 清晰度档位（spec[]）：分辨率可选的唯一来源 ─────────────────────────
#: 客户端接口响应的形状（objectDesc.media[0].spec[]）
SPEC_PAYLOAD = {"data": {"object": {"objectDesc": {"media": [
    {"url": VIDEO_URL, "decodeKey": "123", "spec": [
        {"url": "https://finder.video.qq.com/251/20302/stodownload?s=720",
         "height": 720, "fileFormat": "720P", "decodeKey": "k720"},
        {"url": "https://finder.video.qq.com/251/20302/stodownload?s=1080",
         "height": 1080, "fileFormat": "1080P", "decodeKey": "k1080"},
        {"url": "https://finder.video.qq.com/251/20302/stodownload?s=480",
         "height": 480, "fileFormat": "480P", "decodeKey": "k480"},
    ]}]}}}}


def test_extract_specs_pulls_every_quality():
    specs = wxproxy.extract_specs(SPEC_PAYLOAD)
    heights = sorted(int(s["height"]) for s in specs)
    assert heights == [480, 720, 1080], "三档都要挖出来"
    assert any(s["label"] == "1080P" for s in specs)


def test_store_best_spec_respects_requested_quality(tmp_path):
    """要 1080 就给 1080；要 720 就给 720（不是永远给最高）。"""
    store = wxproxy.CaptureStore(path=tmp_path / "cap.json")
    now = __import__("time").time()
    for height in (480, 720, 1080):
        store.add({"url": f"https://finder.video.qq.com/251/20302/stodownload?s={height}",
                   "decode_key": f"k{height}", "source": "spec", "height": str(height),
                   "label": f"{height}P", "title": f"{height}P", "ts": now})

    assert store.best_spec("1080")["height"] == "1080"
    assert store.best_spec("720")["height"] == "720"
    assert store.best_spec("480")["height"] == "480"
    assert store.best_spec("")["height"] == "1080", "不指定才取最高"
    assert store.best_spec("2160")["height"] == "1080", "要不到就退到最高的可用档"


def test_proxy_records_specs_from_client_response(mitm_env):
    """抓包时客户端响应里的 spec[] 必须被存下来 —— 存下来才谈得上"分辨率可选"。"""
    store, proxy, _tls_port, ca, logs = mitm_env
    _FeedHandler.mode = "spec"
    raw = _proxy_get(proxy.port, TARGET_HOST, 443,
                     "/cgi-bin/micromsg-bin/ilink_findergetcommentdetail", ca)
    assert _split_response(raw)[0] == 200, raw[:120]
    heights = sorted(int(e["height"]) for e in store.entries()
                     if e.get("source") == "spec")
    assert heights == [480, 720, 1080], f"档位没存全：{logs}"


# ── 链式上游代理（真实事故的回归）──────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("http://127.0.0.1:7897", ("127.0.0.1", 7897)),   # ← 真事故：带 scheme
    ("127.0.0.1:7897", ("127.0.0.1", 7897)),
    ("https://127.0.0.1:7897", ("127.0.0.1", 7897)),
    ("http://127.0.0.1:7897/", ("127.0.0.1", 7897)),
    ("127.0.0.1:7897/", ("127.0.0.1", 7897)),
    (":7897", ("127.0.0.1", 7897)),
    ("127.0.0.1", ("127.0.0.1", 80)),
])
def test_upstream_endpoint_parsing(raw, expected):
    """上游地址带不带 scheme 都必须解析对。

    真事故：命令行传的是 ``http://127.0.0.1:7897``，代码按 ``host:port`` 硬切，
    把 ``http://127.0.0.1`` 当成主机名 → 每条非目标请求 getaddrinfo 失败 →
    **用户机器瞬间没网**。解析逻辑必须钉死。
    """
    proxy = wxproxy.ChannelsProxy(wxproxy.CaptureStore(path=None), upstream=raw)
    assert proxy.upstream_endpoint() == expected


class _FakeUpstream(BaseHTTPRequestHandler):
    """最小上游代理：只支持 CONNECT 转发，用来验证链式链路。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_CONNECT(self):                   # noqa: N802
        host, _, port = self.path.partition(":")
        try:
            up = socket.create_connection((host, int(port or 443)), timeout=10)
        except OSError:
            self.send_error(502, "upstream connect failed")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.close_connection = True
        wxproxy._pump(self.connection, up)


def test_chained_upstream_with_scheme(tmp_path):
    """上游用 ``http://host:port`` 写法时，隧道必须真的通。

    这条用例如果早写，就能拦住那次"把用户网弄挂"的事故。
    """
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(5)
    echo_port = echo.getsockname()[1]

    def echo_once() -> None:
        try:
            conn, _ = echo.accept()
            conn.sendall(b"via-upstream:" + conn.recv(1024))
            conn.close()
        except OSError:
            pass

    threading.Thread(target=echo_once, daemon=True).start()

    up_httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    threading.Thread(target=up_httpd.serve_forever, daemon=True).start()
    up_port = int(up_httpd.server_address[1])

    store = wxproxy.CaptureStore(path=tmp_path / "cap.json")
    proxy = wxproxy.ChannelsProxy(store, port=0,
                                  upstream=f"http://127.0.0.1:{up_port}")
    proxy.start()
    try:
        # CONNECT 到回环端口：不在白名单内 → 走隧道 → 交给上游 → 上游连到 echo
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
        sock.sendall(f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n"
                     f"Host: 127.0.0.1:{echo_port}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        assert b" 200" in head.split(b"\r\n")[0], f"隧道没建起来：{head[:120]!r}"
        sock.sendall(b"hello")
        got = sock.recv(1024)
        sock.close()
        assert got == b"via-upstream:hello", f"链式转发没生效：{got!r}"
        assert proxy.stats["tunnel"] >= 1
    finally:
        proxy.stop()
        up_httpd.shutdown()
        up_httpd.server_close()
        echo.close()


def test_proxy_tolerates_client_without_ca(mitm_env):
    """客户端不信任我们的 CA 时，代理不能崩（用户浏览器/别的程序可能连上来）。"""
    store, proxy, _tls_port, ca, logs = mitm_env
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.sendall(f"CONNECT {TARGET_HOST}:443 HTTP/1.1\r\nHost: {TARGET_HOST}\r\n\r\n"
                 .encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    assert b" 200" in head.split(b"\r\n")[0]
    # 不做 TLS 握手，直接关掉 —— 代理侧的握手会失败，但不能把整个服务带崩
    sock.close()
    raw = _proxy_get(proxy.port, TARGET_HOST, 443,
                     "/cgi-bin/mmfinderassistant-bin/feed", ca)
    assert _split_response(raw)[0] == 200, "一个坏客户端不该影响后续请求"
