"""对象存储（S3 兼容）上传通道测试。

这里最值钱的是 ``test_sigv4_matches_aws_test_vector`` —— 它用的是 AWS 官方文档
公开的测试向量。签名算错时 S3 只会回一个 ``SignatureDoesNotMatch``，
不告诉你哪一步错了；有了已知答案才算真的把这个坑堵上。
"""

from __future__ import annotations

import hashlib
import json
import socketserver
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from xydl.config import Config
from xydl.uploaders import build_uploader
from xydl.uploaders.s3 import EMPTY_SHA256, S3Uploader, SigV4, sha256_file, uri_encode

AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """直连的 opener —— 别让系统代理把这些本地请求绕出去。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ══════════════════════════════════════════════════════════════════════
#  SigV4 正确性
# ══════════════════════════════════════════════════════════════════════
def test_sigv4_matches_aws_test_vector():
    """AWS 文档《Signature Calculations ... Single Chunk》里的 GET Object 例子。

    期望签名是官方文档里印出来的那个值，不是我们自己算的 —— 这才叫已知答案。
    """
    signer = SigV4(AWS_ACCESS_KEY, AWS_SECRET_KEY, "us-east-1")
    headers = {
        "host": "examplebucket.s3.amazonaws.com",
        "range": "bytes=0-9",
        "x-amz-content-sha256": EMPTY_SHA256,
        "x-amz-date": "20130524T000000Z",
    }
    canonical = signer.canonical_request("GET", "/test.txt", "", headers, EMPTY_SHA256)
    signature = signer.signature("20130524T000000Z", "20130524", canonical)
    assert signature == (
        "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_sigv4_canonical_request_shape():
    signer = SigV4(AWS_ACCESS_KEY, AWS_SECRET_KEY, "us-east-1")
    headers = {"host": "h", "x-amz-date": "20130524T000000Z"}
    canonical = signer.canonical_request("PUT", "/a/b.mp4", "", headers, EMPTY_SHA256)
    lines = canonical.split("\n")
    assert lines[0] == "PUT"
    assert lines[1] == "/a/b.mp4"
    assert lines[2] == ""                       # 空 query 也要占一行
    assert lines[-1] == EMPTY_SHA256
    assert "host;x-amz-date" in canonical


def test_uri_encode_keeps_slash_and_escapes_chinese():
    assert uri_encode("/apps/视频 1.mp4", keep_slash=True) == "/apps/%E8%A7%86%E9%A2%91%201.mp4"
    assert uri_encode("a/b") == "a%2Fb"          # 不带 keep_slash 时斜杠要编码
    assert uri_encode("A-z_0.9~") == "A-z_0.9~"  # unreserved 原样保留


def test_canonical_query_is_sorted():
    pairs = {"b": "2", "a": "1", "C": "3"}
    assert SigV4.canonical_query(pairs) == "C=3&a=1&b=2"


def test_signing_key_is_deterministic():
    signer = SigV4(AWS_ACCESS_KEY, AWS_SECRET_KEY, "us-east-1")
    assert signer.signing_key("20130524") == signer.signing_key("20130524")
    other = SigV4(AWS_ACCESS_KEY, AWS_SECRET_KEY, "eu-west-1")
    assert signer.signing_key("20130524") != other.signing_key("20130524")


# ══════════════════════════════════════════════════════════════════════
#  假 S3
# ══════════════════════════════════════════════════════════════════════
class FakeS3:
    """假对象存储：收 PUT、**独立校验签名**、记录请求。

    校验用同一套 SigV4 实现重算 —— 这确实不能发现「算法整体错」，
    但能抓住「canonical request 拼错」（路径少个斜杠、漏签了某个头、
    文件没真正发出来）这类最常见的翻车。
    """

    def __init__(self, *, secret_key: str = AWS_SECRET_KEY):
        self.secret_key = secret_key
        self.objects: dict[str, bytes] = {}
        self.requests: list[dict] = []
        self.get_requests: list[dict] = []
        self.signature_ok = True
        self.presign_ok = True
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003
                pass

            def _reply(self, status: int, payload: dict, extra: dict | None = None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parsed = urllib.parse.urlsplit(self.path)

                auth = self.headers.get("Authorization", "")
                ok_signature = outer._verify(auth, parsed.path, self.headers, body)
                outer.requests.append({
                    "path": parsed.path,
                    "content_length": length,
                    "transfer_encoding": self.headers.get("Transfer-Encoding", ""),
                    "authorization": auth,
                    "body": body,
                })
                if not ok_signature:
                    outer.signature_ok = False
                    return self._reply(403, {"error": "SignatureDoesNotMatch"})
                # 按解码后的 key 存，测试里就能用「人话路径」去找对象
                outer.objects[urllib.parse.unquote(parsed.path.lstrip("/"))] = body
                return self._reply(200, {"ok": True}, {"ETag": '"fake-etag"'})

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                key = urllib.parse.unquote(parsed.path.lstrip("/"))
                outer.get_requests.append({"path": parsed.path, "query": query})

                # 假服务默认是**私有桶**：不带预签名的裸请求必须被拒。
                # 这一点很重要 —— 公有桶不校验签名，会把 presign 的 bug 全部藏起来。
                if "X-Amz-Signature" not in query:
                    return self._reply(403, {"error": "AccessDenied"})
                ok, why = outer._verify_presign(parsed.path, query, self.headers)
                if not ok:
                    outer.presign_ok = False
                    return self._reply(403, {"error": "SignatureDoesNotMatch",
                                             "why": why})

                if key not in outer.objects:
                    return self._reply(404, {"error": "NoSuchKey"})
                body = outer.objects[key]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server_cls = type("S", (socketserver.ThreadingTCPServer,), {
            "allow_reuse_address": True,
            "daemon_threads": True,
            "block_on_close": False,
            "handle_error": lambda *a: None,
        })
        self._httpd = server_cls(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         kwargs={"poll_interval": 0.05}).start()

    def _verify(self, auth: str, path: str, headers, body: bytes) -> bool:
        """按请求里声明的 SignedHeaders 重算一遍签名。

        注意先把收到的路径解码：HTTP 请求里传的是百分号编码形式，而
        ``canonical_request`` 自己会再编码一次，直接传进去会双重编码。
        """
        try:
            parts = dict(
                item.strip().split("=", 1)
                for item in auth.split(" ", 1)[1].split(",")
            )
            signed = parts["SignedHeaders"].split(";")
            # Credential 的格式是 <AK>/<日期>/<region>/<service>/aws4_request
            # 第 2 段是**日期**不是 region —— 这里写错的话签名永远对不上
            scope = parts["Credential"].split("/")
            access_key, date_stamp, region = scope[0], scope[1], scope[2]
            signer = SigV4(access_key, self.secret_key, region)
        except Exception:  # noqa: BLE001
            return False

        canonical_headers = {}
        for name in signed:
            if name == "host":
                canonical_headers["host"] = headers.get("Host", "")
            else:
                canonical_headers[name] = headers.get(name, "")
        payload_hash = headers.get("x-amz-content-sha256", "")
        if payload_hash != hashlib.sha256(body).hexdigest():
            return False
        canonical = signer.canonical_request("PUT", urllib.parse.unquote(path), "",
                                             canonical_headers, payload_hash)
        expected = signer.signature(headers.get("x-amz-date", ""), date_stamp, canonical)
        return expected == parts["Signature"]

    def _verify_presign(self, path: str, query: dict,
                        headers) -> tuple[bool, str]:
        """校验预签名 URL —— 用的是 URL 上的查询参数，不是 Authorization 头。

        这一段是补上来的：之前假服务只查 PUT 的签名，不查预签名 GET，
        结果 ``presign`` 漏掉 ``X-Amz-SignedHeaders`` 这种错完全测不出来
        （桶是公有读时更看不出来）。
        """
        params = dict(query)
        signature = params.pop("X-Amz-Signature", "")
        credential = params.get("X-Amz-Credential", "")
        signed_headers = params.get("X-Amz-SignedHeaders", "")
        amz_date = params.get("X-Amz-Date", "")
        if not (credential and signed_headers and amz_date):
            missing = [k for k, v in (
                ("X-Amz-Credential", credential),
                ("X-Amz-SignedHeaders", signed_headers),
                ("X-Amz-Date", amz_date),
            ) if not v]
            return False, f"缺少查询参数：{missing}"

        try:
            scope = credential.split("/")
            signer = SigV4(scope[0], self.secret_key, scope[2])
        except Exception:  # noqa: BLE001
            return False, "X-Amz-Credential 格式不对"

        canonical = "\n".join([
            "GET",
            uri_encode(urllib.parse.unquote(path), keep_slash=True),
            SigV4.canonical_query(params),
            f"host:{headers.get('Host', '')}\n",
            signed_headers,
            "UNSIGNED-PAYLOAD",
        ])
        expected = signer.signature(amz_date, scope[1], canonical)
        return expected == signature, "签名不匹配" if expected != signature else ""

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def fake_s3():
    server = FakeS3()
    yield server
    server.close()


def _s3_config(tmp_path: Path, fake: FakeS3, **overrides) -> Config:
    s3 = {
        "endpoint": fake.base,
        "region": "us-east-1",
        "bucket": "my-bucket",
        "access_key_id": AWS_ACCESS_KEY,
        "secret_access_key": AWS_SECRET_KEY,
        "prefix": "xianyu-video",
        "path_style": True,       # 假服务用路径风格，虚拟主机风格单独测
        "url_expires_sec": 3600,
    }
    s3.update(overrides)
    return Config(
        {
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "download_dir": str(tmp_path / "downloads"),
                "inbox_dir": str(tmp_path / "inbox"),
                "log_dir": str(tmp_path / "logs"),
            },
            "net": {"proxy": "none", "timeout_sec": 5},
            "delivery": {"uploader": "s3", "s3": s3},
        },
        path=tmp_path / "config.json",
    )


# ══════════════════════════════════════════════════════════════════════
#  端到端
# ══════════════════════════════════════════════════════════════════════
def test_s3_upload_end_to_end(tmp_path: Path, fake_s3: FakeS3):
    config = _s3_config(tmp_path, fake_s3)
    config.ensure_dirs()
    payload = bytes(range(256)) * 4096            # 1 MiB
    src = tmp_path / "视频 演示.mp4"
    src.write_bytes(payload)

    result = S3Uploader(config).upload(src, remote_name="7_视频.mp4")

    assert result.ok, result.error
    assert result.remote_path == "s3://my-bucket/xianyu-video/7_视频.mp4"
    # 服务端独立校验签名通过
    assert fake_s3.signature_ok, "签名校验失败，S3 会回 SignatureDoesNotMatch"
    # 字节完整送达（流式发送没截断）
    stored = fake_s3.objects["my-bucket/xianyu-video/7_视频.mp4"]
    assert stored == payload
    assert hashlib.sha256(stored).hexdigest() == sha256_file(src)


def test_s3_upload_sends_content_length_not_chunked(tmp_path: Path, fake_s3: FakeS3):
    """★ 回归测试：必须带 Content-Length。

    urllib 遇到文件对象 body 会退化成 Transfer-Encoding: chunked，
    而 S3 的 PUT 不接受 chunked。曾经真的漏过这一条。
    """
    config = _s3_config(tmp_path, fake_s3)
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 100_000)

    assert S3Uploader(config).upload(src, "a.mp4").ok  # noqa: SIM115

    sent = fake_s3.requests[0]
    assert sent["transfer_encoding"] == ""
    assert sent["content_length"] == 100_000


def test_s3_url_is_presigned(tmp_path: Path, fake_s3: FakeS3):
    config = _s3_config(tmp_path, fake_s3)
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 1024)

    result = S3Uploader(config).upload(src, "a.mp4")

    assert result.ok, result.error
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(result.url).query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["3600"]
    assert query["X-Amz-Signature"][0]
    assert "us-east-1" in query["X-Amz-Credential"][0]


def test_s3_wrong_secret_is_reported(tmp_path: Path, fake_s3: FakeS3):
    """密钥不对时要给出能看懂的错误，而不是一个光秃秃的 403。"""
    config = _s3_config(tmp_path, fake_s3, secret_access_key="错误的密钥")
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 1024)

    result = S3Uploader(config).upload(src, "a.mp4")

    assert not result.ok
    assert "403" in result.error and "SignatureDoesNotMatch" in result.error


def test_s3_presigned_url_actually_downloads(tmp_path: Path, fake_s3: FakeS3):
    """★ 回归测试：预签名链接必须真的能下到东西，裸链接必须被拒。

    之前假服务不校验预签名，`presign` 漏了 `X-Amz-SignedHeaders` 测试也发现不了；
    更糟的是生产桶若是公有读，连「能下」都会骗过自己。
    假服务现在默认按私有桶处理，这条测试才真的有约束力。
    """
    config = _s3_config(tmp_path, fake_s3)
    config.ensure_dirs()
    payload = bytes(range(256)) * 1024            # 256 KiB
    src = tmp_path / "a.mp4"
    src.write_bytes(payload)

    result = S3Uploader(config).upload(src, "a.mp4")
    assert result.ok, result.error
    assert fake_s3.presign_ok, "预签名校验没通过"

    # 买家视角：直接 GET 预签名链接
    with _no_proxy_opener().open(result.url, timeout=10) as resp:
        assert resp.status == 200
        assert resp.read() == payload

    # 裸链接（去掉签名）必须被拒 —— 私有桶的基本要求
    with pytest.raises(urllib.error.HTTPError) as exc:
        _no_proxy_opener().open(result.url.split("?")[0], timeout=10)
    assert exc.value.code == 403


def test_s3_presign_query_carries_signed_headers(tmp_path: Path, fake_s3: FakeS3):
    """★ 回归测试：``X-Amz-SignedHeaders`` 必须出现在查询串里。

    只写在 canonical request 里不算数 —— 服务端得能从 URL 上读到它，
    否则一律 SignatureDoesNotMatch。
    """
    config = _s3_config(tmp_path, fake_s3)
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 32)

    result = S3Uploader(config).upload(src, "a.mp4")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(result.url).query)
    assert query.get("X-Amz-SignedHeaders") == ["host"]
    assert query.get("X-Amz-Algorithm") == ["AWS4-HMAC-SHA256"]
    assert query.get("X-Amz-Credential")


def test_s3_tampered_presign_is_rejected(tmp_path: Path, fake_s3: FakeS3):
    config = _s3_config(tmp_path, fake_s3)
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 32)

    result = S3Uploader(config).upload(src, "a.mp4")
    bad = result.url.replace("X-Amz-Signature=", "X-Amz-Signature=deadbeef")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _no_proxy_opener().open(bad, timeout=10)
    assert exc.value.code == 403


def test_s3_public_base_skips_signing(tmp_path: Path, fake_s3: FakeS3):
    """桶公开 / 挂了 CDN 时直接用永久直链，不做签名。"""
    config = _s3_config(tmp_path, fake_s3,
                        public_base_url="https://cdn.example.com")
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 8)

    result = S3Uploader(config).upload(src, "目录/a.mp4")

    assert result.ok, result.error
    # 中文路径必须百分号编码 —— 带原始中文的 URL 不是合法 URL
    assert result.url == (
        "https://cdn.example.com/xianyu-video/%E7%9B%AE%E5%BD%95/a.mp4"
    )
    assert "X-Amz-Signature" not in result.url


def test_s3_virtual_host_url_has_bucket_in_host(tmp_path: Path, fake_s3: FakeS3):
    """★ 回归测试：虚拟主机风格下，**实际发出去的 URL** 也必须带桶名。

    曾经签名时用了 `bucket.cos.xxx` 作 Host，请求却发到 `cos.xxx` ——
    COS 直接判 NoSuchBucket，报错完全不像签名问题，极难查。
    """
    config = _s3_config(tmp_path, fake_s3, path_style=False,
                        endpoint="https://cos.ap-guangzhou.myqcloud.com",
                        bucket="mybucket-1250000000")
    uploader = S3Uploader(config)

    key = uploader._object_key("目录/a.mp4")
    host, path = uploader._host_and_path(key)
    assert host == "mybucket-1250000000.cos.ap-guangzhou.myqcloud.com"
    assert uploader._url(host, path) == (
        "https://mybucket-1250000000.cos.ap-guangzhou.myqcloud.com"
        "/xianyu-video/%E7%9B%AE%E5%BD%95/a.mp4"
    )


def test_s3_virtual_host_signature_matches_sent_url(tmp_path: Path, fake_s3: FakeS3):
    """签名用的 path 和发出去的 path 必须是同一个，否则签名必然对不上。"""
    config = _s3_config(tmp_path, fake_s3, path_style=False,
                        endpoint="https://cos.ap-guangzhou.myqcloud.com",
                        bucket="mybucket-1250000000")
    uploader = S3Uploader(config)
    key = uploader._object_key("a.mp4")
    host, path = uploader._host_and_path(key)
    signed_path = "/" + key.lstrip("/")
    assert urllib.parse.urlsplit(uploader._url(host, path)).path == signed_path


def test_s3_path_style_puts_bucket_in_path(tmp_path: Path, fake_s3: FakeS3):
    config = _s3_config(tmp_path, fake_s3, path_style=True, bucket="mybucket")
    uploader = S3Uploader(config)
    host, path = uploader._host_and_path(uploader._object_key("a.mp4"))
    assert host == f"127.0.0.1:{fake_s3.port}"
    assert path == "/mybucket/xianyu-video/a.mp4"


def test_s3_missing_config_is_explicit(tmp_path: Path):
    config = Config(
        {"paths": {"data_dir": str(tmp_path), "download_dir": str(tmp_path),
                   "inbox_dir": str(tmp_path), "log_dir": str(tmp_path)},
         "delivery": {"uploader": "s3", "s3": {"endpoint": "", "bucket": ""}}},
        path=tmp_path / "config.json",
    )
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    result = S3Uploader(config).upload(src)
    assert not result.ok
    assert "bucket" in result.error or "access_key" in result.error


def test_s3_missing_file(tmp_path: Path, fake_s3: FakeS3):
    config = _s3_config(tmp_path, fake_s3)
    config.ensure_dirs()
    result = S3Uploader(config).upload(tmp_path / "没有.mp4")
    assert not result.ok and "不存在" in result.error


def test_build_uploader_knows_s3():
    config = Config({"delivery": {"uploader": "s3"}})
    assert isinstance(build_uploader(config), S3Uploader)


def test_describe_mentions_expiry(tmp_path: Path, fake_s3: FakeS3):
    config = _s3_config(tmp_path, fake_s3, url_expires_sec=86400 * 3)
    text = S3Uploader(config).describe()
    assert "3 天" in text
    assert "路径风格" in text
