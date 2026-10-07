"""HTTP 工具：纯标准库实现，支持手动跟随重定向并记录跳转链。

短链展开是核心需求，而 ``urllib`` 默认会静默跟随重定向、把中间地址丢掉，
所以这里自己实现跳转循环，把每一跳都记下来 —— 出问题时能一眼看出卡在哪。
"""

from __future__ import annotations

import gzip
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
#: 服务端可能返回这些状态表示「不支持 HEAD」
HEAD_UNSUPPORTED = frozenset({400, 403, 405, 501, 502})

DEFAULT_MAX_BYTES = 4 * 1024 * 1024

#: ``proxy`` 参数的三态语义（``request`` / ``open_stream`` 都遵守）：
#:   ``None``  → 用系统默认代理（Windows 下读注册表里的 Internet Settings）
#:   ``""``    → 显式禁用代理，直连
#:   ``"http://host:port"`` → 走指定代理
SYSTEM_PROXY = None

#: 配置里 ``net.proxy`` 的写法 → 上面三态
_PROXY_AUTO = {"", "auto", "system", "default", "inherit"}
_PROXY_OFF = {"none", "direct", "off", "no", "-"}


def parse_proxy_spec(spec: object) -> str | None:
    """把配置文件里的代理写法翻译成 :func:`request` 认识的三态。

    >>> parse_proxy_spec("") is None       # 默认：跟随系统代理
    True
    >>> parse_proxy_spec("auto") is None
    True
    >>> parse_proxy_spec("none")           # 强制直连
    ''
    >>> parse_proxy_spec("http://127.0.0.1:7890")
    'http://127.0.0.1:7890'
    """
    if spec is None:
        return SYSTEM_PROXY
    value = str(spec).strip()
    if value.lower() in _PROXY_AUTO:
        return SYSTEM_PROXY
    if value.lower() in _PROXY_OFF:
        return ""
    return value


def system_proxy_url() -> str:
    """读出系统代理的 https/http 地址（yt-dlp 要显式喂给它，它不读注册表）。"""
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 - 读不到代理不算错误
        return ""
    return proxies.get("https") or proxies.get("http") or ""


class HttpError(Exception):
    """网络层错误（超时、DNS、TLS、连接被拒等）。"""

    def __init__(self, message: str, url: str = "", status: int | None = None):
        super().__init__(message)
        self.url = url
        self.status = status


@dataclass
class Response:
    status: int = 0
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    chain: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    @property
    def location(self) -> str:
        return self.headers.get("location", "")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        """按 charset 解码，缺省 utf-8 且不因坏字节炸掉。"""
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype.lower():
            charset = ctype.lower().split("charset=", 1)[1].split(";")[0].strip().strip('"\'')
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """把重定向当成普通响应交回来，由我们决定要不要跟。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _decode_body(raw: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(raw)
        if "deflate" in encoding:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return raw
    return raw


def _build_opener(
    proxy: str | None,
    verify_tls: bool,
    follow: bool = False,
) -> urllib.request.OpenerDirector:
    """proxy 语义：None = 用系统默认代理；"" = 显式禁用代理；其余 = 指定的代理地址。"""
    handlers: list[Any] = [
        urllib.request.HTTPRedirectHandler() if follow else _NoRedirect()
    ]
    if proxy is None:
        pass
    elif proxy == "":
        handlers.append(urllib.request.ProxyHandler({}))
    else:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def request(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    json_body: Any | None = None,
    timeout: float = 15.0,
    proxy: str | None = None,
    verify_tls: bool = True,
    follow: bool = False,
    max_redirects: int = 8,
    max_bytes: int = DEFAULT_MAX_BYTES,
    user_agent: str = DEFAULT_UA,
) -> Response:
    """发一个 HTTP 请求。

    ``follow=True`` 时会手动跟随重定向，``Response.chain`` 里是完整跳转链，
    ``Response.url`` 是最终落地地址 —— 短链展开就靠这个。
    """
    if params:
        parts = urllib.parse.urlsplit(url)
        extra = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        query = "&".join(x for x in (parts.query, extra) if x)
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
        )

    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers = {**(headers or {}), "Content-Type": "application/json; charset=utf-8"}

    send_headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    send_headers.update(headers or {})

    opener = _build_opener(proxy, verify_tls)
    chain: list[str] = []
    current = url
    hops = 0
    current_method = method.upper()
    current_data = data

    while True:
        req = urllib.request.Request(current, data=current_data, method=current_method)
        for key, value in send_headers.items():
            req.add_header(key, value)

        status, resp_headers, body, error = 0, {}, b"", ""
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 0) or resp.getcode()
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                if current_method != "HEAD":
                    raw = resp.read(max_bytes + 1)
                    body = _decode_body(raw, resp_headers.get("content-encoding", ""))
        except urllib.error.HTTPError as exc:
            status = exc.code
            resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            if current_method != "HEAD":
                try:
                    raw = exc.read(max_bytes + 1)
                    body = _decode_body(raw, resp_headers.get("content-encoding", ""))
                except Exception:  # noqa: BLE001 - 读失败不影响状态码
                    body = b""
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.timeout):
                error = f"请求超时（{timeout}s）"
            elif isinstance(reason, ssl.SSLError):
                error = f"TLS 握手失败：{reason}"
            else:
                error = f"网络错误：{reason}"
        except (socket.timeout, TimeoutError):
            error = f"请求超时（{timeout}s）"
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让网络异常炸穿流水线
            error = f"{type(exc).__name__}: {exc}"

        chain.append(current)

        if error:
            return Response(status, current, resp_headers, body, chain, error)

        location = resp_headers.get("location", "")
        if follow and status in REDIRECT_CODES and location and hops < max_redirects:
            current = urllib.parse.urljoin(current, location)
            hops += 1
            if status == 303 or (status in (301, 302) and current_method == "POST"):
                current_method, current_data = "GET", None
            continue

        return Response(status, current, resp_headers, body, chain, "")


def get(url: str, **kwargs: Any) -> Response:
    return request("GET", url, **kwargs)


def head(url: str, **kwargs: Any) -> Response:
    return request("HEAD", url, **kwargs)


def post(url: str, **kwargs: Any) -> Response:
    return request("POST", url, **kwargs)


def open_stream(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    proxy: str | None = None,
    verify_tls: bool = True,
    user_agent: str = DEFAULT_UA,
):
    """打开一个可流式读取的响应（下载大文件用，避免整个读进内存）。

    与 :func:`request` 不同，这里**默认自动跟随重定向** —— 直链下载几乎一定会
    遇到 CDN 的 302。调用方负责关闭返回对象。
    """
    send_headers = {"User-Agent": user_agent, "Accept": "*/*", "Connection": "close"}
    send_headers.update(headers or {})
    req = urllib.request.Request(url, headers=send_headers, method="GET")
    opener = _build_opener(proxy, verify_tls, follow=True)
    return opener.open(req, timeout=timeout)


def get_json(url: str, **kwargs: Any) -> Any:
    resp = request("GET", url, **kwargs)
    if resp.error:
        raise HttpError(resp.error, resp.url)
    if not resp.ok:
        raise HttpError(f"HTTP {resp.status}", resp.url, resp.status)
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise HttpError(f"响应不是合法 JSON：{exc}", resp.url, resp.status) from exc


def post_json(url: str, json_body: Any, **kwargs: Any) -> Any:
    resp = request("POST", url, json_body=json_body, **kwargs)
    if resp.error:
        raise HttpError(resp.error, resp.url)
    if not resp.ok:
        raise HttpError(f"HTTP {resp.status}", resp.url, resp.status)
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise HttpError(f"响应不是合法 JSON：{exc}", resp.url, resp.status) from exc
