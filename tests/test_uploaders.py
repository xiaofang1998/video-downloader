"""上传通道层测试。

全程离线：百度网盘的接口用一个本地假服务替身，所以我们能验证的比「有没有报错」
多得多 —— 分片切得对不对、md5 是不是按序传的、分享失败有没有被翻译成人话。
"""

from __future__ import annotations

import hashlib
import http.server
import json
import re
import socketserver
import threading
import urllib.parse
from pathlib import Path

import pytest

from xydl.config import Config
from xydl.http import encode_multipart
from xydl.uploaders import build_uploader
from xydl.uploaders.baidu import BaiduPanUploader
from xydl.uploaders.base import UploadResult
from xydl.uploaders.none import NullUploader


# ══════════════════════════════════════════════════════════════════════
#  假百度网盘
# ══════════════════════════════════════════════════════════════════════
def _parse_multipart(body: bytes, content_type: str) -> tuple[dict, dict]:
    """从 multipart 请求体里抠出普通字段和文件字段。"""
    match = re.search(r"boundary=([^\s;]+)", content_type or "")
    if not match:
        return {}, {}
    boundary = match.group(1).strip('"')
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for part in body.split(b"--" + boundary.encode()):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        head_text = head.decode("utf-8", "replace")
        name_match = re.search(r'name="([^"]+)"', head_text)
        if not name_match:
            continue
        if "filename=" in head_text:
            files["file"] = data
        else:
            fields[name_match.group(1)] = data.decode("utf-8", "replace")
    return fields, files


class FakeBaidu:
    """假百度网盘：实现上传链路要用到的 5 个接口，并记录全部请求。"""

    def __init__(self, *, share_errno: int = 0):
        self.share_errno = share_errno
        self.calls: list[dict] = []
        self.uploaded_slices: dict[int, bytes] = {}
        self.created: dict = {}
        #: 分享请求里带的文件 id 列表 / 用的参数名 —— 给回归测试用
        self.shared_fsids: list | None = None
        self.share_fid_param: str = ""

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003
                pass

            def _reply(self, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                outer.calls.append({"method": "GET", "path": self.path, "body": b""})
                self._reply({"errno": 0})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                parsed = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                outer.calls.append(
                    {"method": "POST", "path": parsed.path, "query": query, "body": body}
                )

                if parsed.path.endswith("/oauth/2.0/token"):
                    return self._reply({
                        "access_token": "fake-access-token",
                        "refresh_token": "fake-refresh-token",
                        "expires_in": 2592000,
                        "scope": "basic netdisk",
                    })

                if parsed.path.endswith("/xpan/file") and query.get("method") == "precreate":
                    fields = dict(urllib.parse.parse_qsl(body.decode()))
                    blocks = json.loads(fields.get("block_list", "[]"))
                    outer.created["blocks"] = blocks
                    outer.created["path"] = fields.get("path")
                    outer.created["size"] = int(fields.get("size", 0))
                    # 故意只要求上传第 0 和第 2 片，模拟「云端已有部分分片」
                    need = [i for i in range(len(blocks)) if i != 1]
                    return self._reply({
                        "errno": 0, "uploadid": "UP-1", "return_type": 1,
                        "path": fields.get("path", ""), "block_list": need,
                    })

                if parsed.path.endswith("/pcs/superfile2"):
                    _, files = _parse_multipart(body, ctype)
                    partseq = int(query.get("partseq", -1))
                    raw = files.get("file", b"")
                    outer.uploaded_slices[partseq] = raw
                    return self._reply({"md5": hashlib.md5(raw).hexdigest(),
                                        "request_id": 1})

                if parsed.path.endswith("/xpan/file") and query.get("method") == "create":
                    fields = dict(urllib.parse.parse_qsl(body.decode()))
                    outer.created["create_path"] = fields.get("path")
                    return self._reply({"errno": 0, "fs_id": 987654321,
                                        "path": fields.get("path", ""), "size": 0})

                if parsed.path.endswith("/xpan/share"):
                    fields = dict(urllib.parse.parse_qsl(body.decode()))
                    for key in ("fid_list", "fsid_list", "file_id_list"):
                        if key in fields:
                            outer.shared_fsids = json.loads(fields[key])
                            outer.share_fid_param = key
                            break
                    if outer.shared_fsids is None:
                        # 真实接口在缺文件 id 时返回 errno=2（参数错误），
                        # 而不是权限错误 —— 这个假服务照搬这个行为，
                        # 免得哪天又把 fid 参数漏掉。
                        return self._reply({"errno": 2, "shareid": -1, "link": ""})
                    return self._reply({
                        "errno": outer.share_errno,
                        "link": "https://pan.baidu.com/s/1FAKE" if not outer.share_errno else "",
                        "pwd": "ab12" if not outer.share_errno else "",
                        "shareid": 1,
                    })

                self._reply({"errno": 0})

        self._handler = Handler
        # block_on_close=False 不是可有可无的：HTTP/1.1 的 handler 线程会挂在
        # keep-alive 的 readline 上，默认的 server_close() 会 join 它们，
        # 一个测试白等十几秒。关掉之后测完即走。
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


@pytest.fixture
def fake_baidu():
    server = FakeBaidu()
    yield server
    server.close()


def _baidu_config(tmp_path: Path, fake: FakeBaidu, **overrides) -> Config:
    baidu = {
        "app_key": "APPKEY123456",
        "secret_key": "SECRET",
        "refresh_token": "REFTOKEN",
        "remote_dir": "/apps/xianyu",
        "slice_mb": 1,
        "upload_host": fake.base,
        "share_api": "old",
        "endpoints": {
            "oauth_token": f"{fake.base}/oauth/2.0/token",
            "oauth_authorize": f"{fake.base}/oauth/2.0/authorize",
            "xpan_file": f"{fake.base}/rest/2.0/xpan/file",
            "xpan_share": f"{fake.base}/rest/2.0/xpan/share",
            "share_new": f"{fake.base}/apaas/1.0/share/set",
            "xpan_nas": f"{fake.base}/rest/2.0/xpan/nas",
        },
    }
    baidu.update(overrides)
    return Config(
        {
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "download_dir": str(tmp_path / "downloads"),
                "inbox_dir": str(tmp_path / "inbox"),
                "log_dir": str(tmp_path / "logs"),
            },
            "net": {"proxy": "none", "timeout_sec": 5, "verify_tls": True},
            "delivery": {"uploader": "baidu", "baidu": baidu},
        },
        path=tmp_path / "config.json",
    )


# ══════════════════════════════════════════════════════════════════════
#  multipart 与结果对象
# ══════════════════════════════════════════════════════════════════════
def test_multipart_roundtrip():
    """自己拼的 multipart 得能被标准方式解回来 —— 否则网盘一定收不到文件。"""
    body, boundary = encode_multipart(
        {"method": "upload", "partseq": 0},
        [("file", "blob0", b"\x00\x01\x02hello")],
    )
    fields, files = _parse_multipart(
        body, f"multipart/form-data; boundary={boundary}"
    )
    assert fields == {"method": "upload", "partseq": "0"}
    assert files["file"] == b"\x00\x01\x02hello"


def test_reply_line_with_pwd():
    result = UploadResult(ok=True, url="https://pan.baidu.com/s/abc", pwd="ab12")
    text = result.reply_line()
    assert "https://pan.baidu.com/s/abc" in text
    assert "ab12" in text


def test_reply_line_without_pwd():
    """没有提取码时不能出现一句空落落的「提取码：」。"""
    result = UploadResult(ok=True, url="https://pan.baidu.com/s/abc")
    text = result.reply_line()
    assert "提取码" not in text
    assert "https://pan.baidu.com/s/abc" in text


def test_reply_line_failed_is_empty():
    assert UploadResult(ok=False, error="炸了").reply_line() == ""


def test_reply_line_template():
    result = UploadResult(ok=True, url="U", pwd="P")
    assert result.reply_line("点这里 {url} 码 {pwd}") == "点这里 U 码 P"


def test_build_uploader_unknown_falls_back():
    """通道名写错不能把服务弄挂 —— 退回 none，最差只是不发链接。"""
    config = Config({"delivery": {"uploader": "不存在的网盘"}})
    assert isinstance(build_uploader(config), NullUploader)


def test_null_uploader_fails_softly():
    result = NullUploader(Config({})).upload(__file__)
    assert not result.ok and result.error


# ══════════════════════════════════════════════════════════════════════
#  百度网盘：完整链路
# ══════════════════════════════════════════════════════════════════════
def test_baidu_upload_end_to_end(tmp_path: Path, fake_baidu: FakeBaidu):
    """2.5MB / 1MB 分片 → 3 片，且只补传云端缺的那两片。"""
    config = _baidu_config(tmp_path, fake_baidu)
    config.ensure_dirs()
    payload = bytes(range(256)) * (10240)          # 2.5 MiB
    src = tmp_path / "视频 演示.mp4"
    src.write_bytes(payload)

    uploader = BaiduPanUploader(config)
    result = uploader.upload(src, remote_name="7_视频.mp4")

    assert result.ok, result.error
    assert result.url == "https://pan.baidu.com/s/1FAKE"
    assert result.pwd == "ab12"
    assert result.remote_path.endswith("7_视频.mp4")
    assert result.fs_id == "987654321"

    # 目录 + 文件名拼对了（中文不能被 urlencode 弄丢）
    assert fake_baidu.created["path"] == "/apps/xianyu/7_视频.mp4"

    # 三片：0 / 1 / 2；假服务说 1 云端已有，所以只该收到 0 和 2
    blocks = fake_baidu.created["blocks"]
    assert len(blocks) == 3
    assert set(fake_baidu.uploaded_slices) == {0, 2}

    # 传上去的字节必须和本地切片逐字节一致（顺序错了这里立刻暴露）
    slice_bytes = 1024 * 1024
    assert fake_baidu.uploaded_slices[0] == payload[:slice_bytes]
    assert fake_baidu.uploaded_slices[2] == payload[2 * slice_bytes:]
    assert hashlib.md5(fake_baidu.uploaded_slices[0]).hexdigest() == blocks[0]


def test_baidu_share_carries_file_id(tmp_path: Path, fake_baidu: FakeBaidu):
    """★ 回归测试：分享请求必须带上文件 id。

    漏掉它会得到 errno=2（参数错误），而错误文案看起来像「没有分享权限」，
    极容易误判成要去申请付费能力 —— 这个坑真的踩过一次。
    """
    config = _baidu_config(tmp_path, fake_baidu)
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * (1024 * 1024))

    result = BaiduPanUploader(config).upload(src, remote_name="1_a.mp4")

    assert result.ok, result.error
    assert fake_baidu.share_fid_param, "分享请求里一个文件 id 参数都没带"
    assert fake_baidu.shared_fsids == ["987654321"]
    assert result.url == "https://pan.baidu.com/s/1FAKE"


def test_baidu_reuses_valid_access_token(tmp_path: Path, fake_baidu: FakeBaidu):
    """access_token 没过期就不该反复去刷 —— 刷太勤会被限流。"""
    config = _baidu_config(tmp_path, fake_baidu)
    config.ensure_dirs()
    uploader = BaiduPanUploader(config)
    first = uploader.access_token()
    second = uploader.access_token()
    assert first == second == "fake-access-token"
    token_calls = [c for c in fake_baidu.calls
                   if c["path"].endswith("/oauth/2.0/token")]
    assert len(token_calls) == 1


def test_baidu_missing_appkey_is_explicit(tmp_path: Path, fake_baidu: FakeBaidu):
    config = _baidu_config(tmp_path, fake_baidu, app_key="")
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 10)
    result = BaiduPanUploader(config).upload(src)
    assert not result.ok
    assert "app_key" in result.error


def test_baidu_upload_missing_file(tmp_path: Path, fake_baidu: FakeBaidu):
    config = _baidu_config(tmp_path, fake_baidu)
    config.ensure_dirs()
    result = BaiduPanUploader(config).upload(tmp_path / "不存在.mp4")
    assert not result.ok and "不存在" in result.error


def test_baidu_no_refresh_token_says_how_to_fix(tmp_path: Path, fake_baidu: FakeBaidu):
    config = _baidu_config(tmp_path, fake_baidu, refresh_token="")
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 10)
    result = BaiduPanUploader(config).upload(src)
    assert not result.ok
    assert "baidu-login" in result.error


def test_baidu_upload_only_skips_share_entirely(tmp_path: Path, fake_baidu: FakeBaidu):
    """upload_only 模式下不该去碰分享接口 —— 那是注定失败的调用。"""
    config = _baidu_config(tmp_path, fake_baidu)
    config.set("delivery.mode", "upload_only")
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * (1024 * 1024))

    result = BaiduPanUploader(config).upload(src)

    assert result.ok, result.error          # 传上去了就算成功
    assert result.fs_id == "987654321"
    assert result.url == ""                 # 但本来就没有链接
    assert fake_baidu.share_fid_param == ""  # 分享接口一次都没被调用


def test_baidu_errno2_explains_the_real_reason(tmp_path: Path, fake_baidu: FakeBaidu):
    """★ errno=2 官方写着「参数错误」，实际是「没开通分享服务」。

    照着「参数错误」去调参数会白折腾半天（这个坑真踩过），所以文案必须点破。
    """
    fake_baidu.share_errno = 2
    config = _baidu_config(tmp_path, fake_baidu)
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 1024)

    result = BaiduPanUploader(config).upload(src)

    assert not result.ok
    assert "文件分享服务" in result.error
    assert "企业开发者" in result.error
    # 文件本身传上去了，这个事实不能丢
    assert result.remote_path or result.fs_id


def test_baidu_share_failure_is_translated(tmp_path: Path, fake_baidu: FakeBaidu):
    """分享接口失败时，文案里要说清「文件其实传上去了」，别让人以为是上传挂了。"""
    fake_baidu.share_errno = -6
    config = _baidu_config(tmp_path, fake_baidu)
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 1024)
    result = BaiduPanUploader(config).upload(src)
    assert not result.ok
    assert "分享" in result.error
    # 但文件确实传完了 —— create 被调过
    assert fake_baidu.created.get("create_path")


def test_baidu_network_error_does_not_raise(tmp_path: Path):
    """连不上时必须返回结果对象，不能把异常抛进流水线。"""
    config = Config(
        {
            "paths": {"data_dir": str(tmp_path / "d"), "download_dir": str(tmp_path / "dl"),
                      "inbox_dir": str(tmp_path / "i"), "log_dir": str(tmp_path / "l")},
            "net": {"proxy": "none", "timeout_sec": 1},
            "delivery": {"uploader": "baidu", "baidu": {
                "app_key": "K", "secret_key": "S", "refresh_token": "R",
                "upload_host": "http://127.0.0.1:1",
                "endpoints": {
                    "oauth_token": "http://127.0.0.1:1/oauth/2.0/token",
                    "xpan_file": "http://127.0.0.1:1/rest/2.0/xpan/file",
                    "xpan_share": "http://127.0.0.1:1/rest/2.0/xpan/share",
                },
            }},
        },
        path=tmp_path / "config.json",
    )
    config.ensure_dirs()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 16)
    result = BaiduPanUploader(config).upload(src)
    assert not result.ok and result.error
