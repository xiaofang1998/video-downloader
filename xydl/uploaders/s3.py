"""S3 兼容对象存储：预签名直链交付。

一套实现覆盖所有 S3 兼容的存储，换服务商只改 endpoint：

| 服务商 | endpoint 形态 | region 示例 |
| --- | --- | --- |
| 腾讯云 COS | ``https://cos.<region>.myqcloud.com`` | ``ap-guangzhou`` |
| 阿里云 OSS | ``https://s3.oss-<region>.aliyuncs.com`` | ``oss-cn-hangzhou`` |
| Cloudflare R2 | ``https://<accountid>.r2.cloudflarestorage.com`` | ``auto`` |
| MinIO（自建） | ``http://192.168.1.10:9000`` | ``us-east-1`` |
| 七牛云 | ``https://s3-<region>.qiniucdn.com`` | ``cn-east-1`` |

为什么走预签名链接而不是把桶设成公开：预签名链接**带有效期**，
过期自动失效，不用为一个代下载业务把整个桶敞着。默认 7 天。

签名算法是 AWS Signature Version 4，**纯标准库实现**，没有 boto3 依赖 ——
这个项目连 yt-dlp 之外都不引第三方库，不该为了传个文件破例。
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import http as http_util
from ..config import Config
from .base import UploadResult, Uploader

SERVICE = "s3"
ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

#: 这些字符在 SigV4 里不编码（RFC 3986 unreserved）
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
)


def uri_encode(value: str, *, keep_slash: bool = False) -> str:
    """SigV4 要求的 URI 编码。

    **按 UTF-8 字节编码，不是按字符编码。** 直接对 ``ord(ch)`` 做 %XX 会把中文
    编成 ``%89C6`` 这种四不像（字符码点不是字节），S3 那边算出来的 canonical
    request 跟你就对不上，只会回一个 SignatureDoesNotMatch。
    """
    out: list[str] = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        if char in _UNRESERVED:
            out.append(char)
        elif byte == 0x2F and keep_slash:      # '/'
            out.append("/")
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """流式算文件 SHA256（大文件不能整个读进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


class SigV4:
    """AWS Signature Version 4 签名器。

    刻意把签名过程拆成纯函数（只依赖传入的字符串），这样可以用 AWS 官方文档里
    公开的测试向量做已知答案验证 —— 签名算错的话，S3 只会回一个
    ``SignatureDoesNotMatch``，根本看不出是哪里错。
    """

    def __init__(self, access_key: str, secret_key: str, region: str,
                 service: str = SERVICE, session_token: str = ""):
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "us-east-1"
        self.service = service
        self.session_token = session_token

    # ── 基础件 ─────────────────────────────────────────────────────
    @staticmethod
    def amz_date(ts: float | None = None) -> tuple[str, str]:
        """返回 (20130524T000000Z, 20130524)。"""
        moment = datetime.fromtimestamp(ts if ts is not None else time.time(),
                                        tz=timezone.utc)
        return moment.strftime("%Y%m%dT%H%M%SZ"), moment.strftime("%Y%m%d")

    def signing_key(self, date_stamp: str) -> bytes:
        k_date = _hmac(("AWS4" + self.secret_key).encode("utf-8"), date_stamp)
        k_region = _hmac(k_date, self.region)
        k_service = _hmac(k_region, self.service)
        return _hmac(k_service, "aws4_request")

    def credential_scope(self, date_stamp: str) -> str:
        return f"{date_stamp}/{self.region}/{self.service}/aws4_request"

    @staticmethod
    def canonical_query(pairs: dict[str, Any]) -> str:
        encoded = [
            (uri_encode(str(k)), uri_encode(str(v)))
            for k, v in pairs.items() if v is not None
        ]
        encoded.sort()
        return "&".join(f"{k}={v}" for k, v in encoded)

    @staticmethod
    def canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
        items = sorted((k.lower().strip(), " ".join(str(v).split()))
                       for k, v in headers.items())
        canonical = "".join(f"{k}:{v}\n" for k, v in items)
        return canonical, ";".join(k for k, _ in items)

    @staticmethod
    def canonical_request(method: str, path: str, query: str,
                          headers: dict[str, str], payload_hash: str) -> str:
        canonical_header_block, signed_headers = SigV4.canonical_headers(headers)
        return "\n".join([
            method.upper(),
            uri_encode(path, keep_slash=True),
            query,
            canonical_header_block,
            signed_headers,
            payload_hash,
        ])

    def string_to_sign(self, amz_date: str, date_stamp: str,
                       canonical_request: str) -> str:
        return "\n".join([
            ALGORITHM,
            amz_date,
            self.credential_scope(date_stamp),
            sha256_hex(canonical_request.encode("utf-8")),
        ])

    def signature(self, amz_date: str, date_stamp: str, canonical_request: str) -> str:
        key = self.signing_key(date_stamp)
        return hmac.new(
            key, self.string_to_sign(amz_date, date_stamp, canonical_request).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # ── 两种用法 ───────────────────────────────────────────────────
    def authorization(self, method: str, path: str, query: str,
                      headers: dict[str, str], payload_hash: str,
                      amz_date: str, date_stamp: str) -> str:
        """给请求头签一个 Authorization（用于 PUT 上传）。"""
        request = self.canonical_request(method, path, query, headers, payload_hash)
        _, signed_headers = self.canonical_headers(headers)
        signature = self.signature(amz_date, date_stamp, request)
        return (
            f"{ALGORITHM} Credential={self.access_key}/{self.credential_scope(date_stamp)}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

    def presign(self, method: str, host: str, path: str, expires: int,
                amz_date: str, date_stamp: str) -> str:
        """给 URL 签查询串（用于生成买家可点的下载链接）。

        ``X-Amz-SignedHeaders`` **必须作为查询参数出现**，不能只放在
        canonical request 里 —— 少了它，服务端不知道哪些头参与了签名，
        直接判 SignatureDoesNotMatch。
        """
        signed_header_names = "host"
        pairs: dict[str, Any] = {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": f"{self.access_key}/{self.credential_scope(date_stamp)}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(max(1, int(expires))),
            "X-Amz-SignedHeaders": signed_header_names,
        }
        if self.session_token:
            pairs["X-Amz-Security-Token"] = self.session_token

        query = self.canonical_query(pairs)
        canonical_request = "\n".join([
            method.upper(),
            uri_encode(path, keep_slash=True),
            query,
            f"host:{host}\n",
            signed_header_names,
            UNSIGNED_PAYLOAD,
        ])
        signature = self.signature(amz_date, date_stamp, canonical_request)
        return f"{query}&X-Amz-Signature={signature}"


class S3Uploader(Uploader):
    name = "s3"
    label = "对象存储（S3 兼容）"

    def __init__(self, config: Config):
        super().__init__(config)
        cfg = config.get("delivery.s3", {}) or {}
        self.endpoint = str(cfg.get("endpoint") or "").strip().rstrip("/")
        self.region = str(cfg.get("region") or "us-east-1").strip()
        self.bucket = str(cfg.get("bucket") or "").strip()
        self.access_key = str(cfg.get("access_key_id") or "").strip()
        self.secret_key = str(cfg.get("secret_access_key") or "").strip()
        self.session_token = str(cfg.get("session_token") or "").strip()
        self.prefix = str(cfg.get("prefix") or "").strip().strip("/")
        #: MinIO / 部分私有实现不支持 virtual-host 风格，用路径风格
        self.path_style = bool(cfg.get("path_style", False))
        self.expires = max(60, int(cfg.get("url_expires_sec", 604800) or 604800))
        #: 桶本身公开（或挂了 CDN）时填这个，直接用永久直链，不做签名
        self.public_base = str(cfg.get("public_base_url") or "").strip().rstrip("/")
        self.acl = str(cfg.get("acl") or "").strip()

        self.proxy = http_util.parse_proxy_spec(config.get("net.proxy"))
        self.verify_tls = bool(config.get("net.verify_tls", True))
        self.timeout = float(config.get("net.timeout_sec", 15) or 15)

        self.signer = SigV4(self.access_key, self.secret_key, self.region,
                            SERVICE, self.session_token)

    # ── 地址拼装 ───────────────────────────────────────────────────
    def _split_endpoint(self) -> tuple[str, str, str]:
        parsed = urllib.parse.urlsplit(self.endpoint)
        return parsed.scheme or "https", parsed.netloc, parsed.path.rstrip("/")

    def _host_and_path(self, key: str) -> tuple[str, str]:
        _, host, base_path = self._split_endpoint()
        key_path = "/" + key.lstrip("/")
        if self.path_style or not host:
            path = f"{base_path}/{self.bucket}{key_path}"
        else:
            host = f"{self.bucket}.{host}"
            path = f"{base_path}{key_path}"
        return host, path

    def _url(self, host: str, path: str) -> str:
        """拼出真正要发的 URL。

        **必须用签名时那个 host**（虚拟主机风格下落了桶名），不能直接用
        ``self.endpoint`` —— 否则签名的 Host 是 ``bucket.cos.xxx``、
        请求却发到 ``cos.xxx``，COS/S3 会判成 NoSuchBucket，
        而且报错长得完全不像签名问题，特别难查。
        """
        scheme, _, _ = self._split_endpoint()
        return f"{scheme}://{host}{uri_encode(path, keep_slash=True)}"

    def _object_key(self, remote_name: str) -> str:
        name = (remote_name or "").strip().lstrip("/")
        return f"{self.prefix}/{name}" if self.prefix else name

    def _public_url(self, key: str) -> str:
        if self.public_base:
            return f"{self.public_base}/{uri_encode(key, keep_slash=True)}"
        return ""

    # ── 上传 ───────────────────────────────────────────────────────
    def put_object(self, key: str, path: Path) -> dict:
        host, object_path = self._host_and_path(key)
        payload_hash = sha256_file(path)
        amz_date, date_stamp = SigV4.amz_date()

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {
            "host": host,
            "content-type": content_type,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        if self.acl:
            headers["x-amz-acl"] = self.acl

        authorization = self.signer.authorization(
            "PUT", object_path, "", headers, payload_hash, amz_date, date_stamp
        )

        send_headers = {k: v for k, v in headers.items() if k != "host"}
        send_headers["Authorization"] = authorization
        # Content-Length 必须显式给：urllib 遇到「文件对象」这种 body 会算不出长度，
        # 于是退回 Transfer-Encoding: chunked —— 而 S3 的 PUT 不接受 chunked，
        # 会直接报 411/501。给了长度它才会老老实实流式发。
        # 注意它**不进 SignedHeaders**，所以不用参与签名。
        send_headers["Content-Length"] = str(path.stat().st_size)

        url = self._url(host, object_path)
        # data 传文件对象：urllib 会流式发送并按 seek/tell 自动算出 Content-Length，
        # 不会把整个视频读进内存。
        with open(path, "rb") as fh:
            resp = http_util.request(
                "PUT", url, data=fh, headers=send_headers,
                timeout=max(120.0, self.timeout * 4),
                proxy=self.proxy, verify_tls=self.verify_tls,
                max_bytes=64 * 1024,
            )
        if resp.error:
            raise RuntimeError(f"上传失败（网络）：{resp.error}")
        if resp.status >= 400:
            raise RuntimeError(
                f"上传失败：HTTP {resp.status} {resp.text[:240]}"
            )
        return {
            "etag": resp.headers.get("etag", ""),
            "payload_sha256": payload_hash,
            "content_type": content_type,
        }

    # ── 对外 ───────────────────────────────────────────────────────
    def upload(self, path: str | Path, remote_name: str = "") -> UploadResult:
        src = Path(path)
        if not src.is_file():
            return UploadResult(ok=False, error=f"文件不存在：{src}")
        if not (self.endpoint and self.bucket and self.access_key and self.secret_key):
            return UploadResult(
                ok=False,
                error=("对象存储没配全：需要 delivery.s3.endpoint / bucket / "
                       "access_key_id / secret_access_key"),
            )
        try:
            key = self._object_key(remote_name or src.name)
            info = self.put_object(key, src)

            public = self._public_url(key)
            if public:
                return UploadResult(ok=True, url=public, remote_path=f"s3://{self.bucket}/{key}",
                                    fs_id=key, raw=info)

            # 必须用 _host_and_path 给出的 object_path，不能自己拼 "/"+key：
            # 路径风格（MinIO 等）下它前面还有 /<bucket> 一段，漏了就 404。
            host, object_path = self._host_and_path(key)
            amz_date, date_stamp = SigV4.amz_date()
            query = self.signer.presign("GET", host, object_path,
                                        self.expires, amz_date, date_stamp)
            url = f"{self._url(host, object_path)}?{query}"
            return UploadResult(
                ok=True, url=url, remote_path=f"s3://{self.bucket}/{key}",
                fs_id=key, raw={**info, "expires_sec": self.expires},
            )
        except Exception as exc:  # noqa: BLE001 - 上传绝不能炸穿交付流程
            return UploadResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def describe(self) -> str:
        if not self.endpoint:
            return f"{self.label}（未配置 endpoint）"
        style = "路径风格" if self.path_style else "虚拟主机风格"
        days = self.expires / 86400
        return (f"{self.label} {self.endpoint} / {self.bucket}"
                f"（{style}，链接 {days:.0f} 天有效）")
