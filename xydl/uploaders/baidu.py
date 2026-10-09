"""百度网盘开放平台上传 + 生成分享链接。

走的是官方 XPAN 接口（不是逆向手机端协议），三步式分片上传：

    precreate   预上传，拿 uploadid（顺便告诉云端哪些分片已经有了）
    superfile2  逐片上传，普通用户单分片固定 4MB
    create      合并成文件，拿 fs_id

然后调分享接口把 fs_id 换成一个能发出去的链接。

──── 需要你自己准备的三样东西 ────────────────────────────────────────
1. AppKey / SecretKey —— 百度网盘开放平台控制台建应用后拿到
2. refresh_token      —— 跑 `run.py baidu-login` 走一遍 OAuth 授权
3. 上传权限           —— 个人开发者需在开放平台申请开通「上传」能力

──── 两个真实的坑 ────────────────────────────────────────────────────
* **上传域名不是固定的**。官方文档要求先查「上传域名」，但那个接口在各版本
  文档里说法不一。这里改成按候选域名列表逐个试、记住成功的那个，比猜一个
  写死要稳。
* **分享接口可能调不通**。新版文件分享服务（``/apaas/1.0/share/set``）是
  企业开发者 + 付费能力；旧版（``/rest/2.0/xpan/share?method=set``）能否用
  取决于你的应用资质。所以这里两种都试，都失败就明确报错，
  由上层退回 ``delivery.mode = upload_only``（只上传、不发链接）。
"""

from __future__ import annotations

import hashlib
import json
import random
import string
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .. import http as http_util
from ..config import Config
from .base import UploadResult, Uploader

OAUTH_TOKEN = "https://openapi.baidu.com/oauth/2.0/token"
OAUTH_AUTHORIZE = "https://openapi.baidu.com/oauth/2.0/authorize"
XPAN_FILE = "https://pan.baidu.com/rest/2.0/xpan/file"
XPAN_SHARE = "https://pan.baidu.com/rest/2.0/xpan/share"
XPAN_NAS = "https://pan.baidu.com/rest/2.0/xpan/nas"
SHARE_NEW = "https://pan.baidu.com/apaas/1.0/share/set"

#: 候选上传域名，按顺序试，成功一个就记住。可用 delivery.baidu.upload_host 覆盖。
DEFAULT_UPLOAD_HOSTS = (
    "https://d.pcs.baidu.com",
    "https://c3.pcs.baidu.com",
    "https://c2.pcs.baidu.com",
    "https://c1.pcs.baidu.com",
)

#: 旧版分享接口的「文件 id 列表」参数名在不同版本文档里叫法不同，逐个试。
_SHARE_FID_KEYS = ("fid_list", "fsid_list", "file_id_list")

#: 百度常见的「需要重新授权」错误码
_AUTH_ERRORS = frozenset({-6, 110, 111, 31064, 31066, 31067})

#: 分享接口的错误码 → 人话。**errno=2 是这里最容易误判的一个**：
#: 官方错误表把它写成「参数错误」，但实测在参数完全正确的情况下也会返回它，
#: 真实原因是这个应用没有开通「文件分享服务」（新版是企业开发者 + 付费能力）。
#: 照着「参数错误」去调参数会白折腾半天，所以这里直接说清楚。
_SHARE_ERRNO_HINT: dict[int, str] = {
    2: ("这个应用没有开通「文件分享服务」。注意：官方错误表把 errno=2 写成"
        "「参数错误」，但实测参数正确时也会返回它 —— 真正的原因是分享能力没批。"
        "新版文件分享服务是企业开发者专属的付费能力"
        "（见 pan.baidu.com/union → 文件分享服务简介）。"
        "过渡方案：把 delivery.mode 改成 upload_only，或换个交付通道"),
    -1: "该文件的分享功能被平台禁用（通常是内容被判定为违规）",
    -10: "分享外链已达上限（10 万条）",
    -16: "该文件被限制分享",
    -17: "分享数量超过限制",
    13998: "invalid app —— 该应用未开通文件分享服务（企业开发者 + 付费能力）",
}


def explain_share_errno(errno: Any) -> str:
    try:
        code = int(errno)
    except (TypeError, ValueError):
        return f"errno={errno}"
    hint = _SHARE_ERRNO_HINT.get(code)
    return f"errno={code}：{hint}" if hint else f"errno={code}"


def _md5_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def random_pwd(length: int = 4) -> str:
    """百度分享提取码只认 4 位小写字母数字。"""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


class BaiduError(RuntimeError):
    """百度接口返回了一个明确的错误。"""


class BaiduPanUploader(Uploader):
    name = "baidu"
    label = "百度网盘"

    def __init__(self, config: Config):
        super().__init__(config)
        cfg = config.get("delivery.baidu", {}) or {}
        self.app_key = str(cfg.get("app_key") or "").strip()
        self.secret_key = str(cfg.get("secret_key") or "").strip()
        self.refresh_token = str(cfg.get("refresh_token") or "").strip()
        self.remote_dir = self._norm_dir(str(cfg.get("remote_dir") or "/apps/xianyu-video"))
        self.share_period_days = int(cfg.get("share_period_days", 7) or 7)
        self.share_api = str(cfg.get("share_api") or "old").strip().lower()
        self.share_pwd = str(cfg.get("share_pwd") or "").strip()
        # upload_only = 只要文件在网盘里，不要链接。这时**直接跳过分享请求**，
        # 省掉一次注定失败的调用（也省得日志里天天刷权限错误）。
        self.share_enabled = (
            str(config.get("delivery.mode", "share_link") or "").strip().lower()
            != "upload_only"
        )
        self.slice_mb = max(1, min(int(cfg.get("slice_mb", 4) or 4), 32))
        host = str(cfg.get("upload_host") or "").strip()
        self.upload_hosts = (host,) if host else DEFAULT_UPLOAD_HOSTS
        self._upload_host = ""

        # 接口地址留成实例属性：既方便某些部署换域名，也让测试能指向本地假服务。
        endpoints = cfg.get("endpoints") if isinstance(cfg.get("endpoints"), dict) else {}
        self.oauth_token_url = str(endpoints.get("oauth_token") or OAUTH_TOKEN)
        self.oauth_authorize_url = str(endpoints.get("oauth_authorize") or OAUTH_AUTHORIZE)
        self.xpan_file_url = str(endpoints.get("xpan_file") or XPAN_FILE)
        self.xpan_share_url = str(endpoints.get("xpan_share") or XPAN_SHARE)
        self.xpan_nas_url = str(endpoints.get("xpan_nas") or XPAN_NAS)
        self.share_new_url = str(endpoints.get("share_new") or SHARE_NEW)

        self.proxy = http_util.parse_proxy_spec(config.get("net.proxy"))
        self.verify_tls = bool(config.get("net.verify_tls", True))
        self.timeout = float(config.get("net.timeout_sec", 15) or 15)
        self.token_path = config.path_of("data_dir") / "baidu_token.json"

    # ── 工具 ───────────────────────────────────────────────────────
    @staticmethod
    def _norm_dir(path: str) -> str:
        path = "/" + path.strip("/")
        return path or "/"

    def _call(self, method: str, url: str, *, params: dict | None = None,
              data: dict | None = None, timeout: float | None = None) -> dict:
        """调一个返回 JSON 的百度接口。网络层错误和业务 errno 都抛 BaiduError。"""
        body = None
        headers = {}
        if data is not None:
            body = urllib.parse.urlencode(
                {k: v for k, v in data.items() if v is not None}
            ).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        resp = http_util.request(
            method, url, params=params or {}, data=body, headers=headers,
            timeout=timeout or self.timeout, proxy=self.proxy,
            verify_tls=self.verify_tls,
        )
        if resp.error:
            raise BaiduError(f"网络错误：{resp.error}")
        if resp.status >= 400:
            raise BaiduError(f"HTTP {resp.status}：{resp.text[:200]}")
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise BaiduError(f"响应不是合法 JSON：{resp.text[:200]}") from exc
        if not isinstance(payload, dict):
            raise BaiduError(f"响应格式意外：{payload!r}")
        return payload

    @staticmethod
    def _check(errno: Any, payload: dict, what: str) -> None:
        if int(errno or 0) != 0:
            if int(errno or 0) in _AUTH_ERRORS:
                raise BaiduError(
                    f"{what} 失败：授权已失效或权限不足（errno={errno}）。"
                    f"跑 `run.py baidu-login --force` 重新授权，"
                    f"并确认应用已开通上传能力"
                )
            raise BaiduError(f"{what} 失败：errno={errno}，响应={payload}")

    # ── 授权 ───────────────────────────────────────────────────────
    def authorize_url(self, qrcode: bool = False) -> str:
        """拼出 OAuth 授权页地址。用户在上面同意后拿到 code。"""
        params = {
            "response_type": "code",
            "client_id": self.app_key,
            "redirect_uri": "oob",
            "scope": "basic,netdisk",
            "display": "page",
        }
        if qrcode:
            params["qrcode"] = "1"
        return f"{self.oauth_authorize_url}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str) -> dict:
        """用授权码换 refresh_token，并落盘。"""
        if not self.app_key or not self.secret_key:
            raise BaiduError("还没配 delivery.baidu.app_key / secret_key")
        payload = self._call(
            "POST", self.oauth_token_url,
            data={
                "grant_type": "authorization_code",
                "code": code.strip(),
                "client_id": self.app_key,
                "client_secret": self.secret_key,
                "redirect_uri": "oob",
            },
            timeout=30,
        )
        if "refresh_token" not in payload:
            raise BaiduError(
                f"换取 token 失败：{payload.get('error_description') or payload}"
            )
        self._save_token(payload)
        self.refresh_token = payload["refresh_token"]
        return payload

    def _save_token(self, payload: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        expires_in = int(payload.get("expires_in", 2592000) or 2592000)
        record = {
            "access_token": payload.get("access_token", ""),
            "refresh_token": payload.get("refresh_token", self.refresh_token),
            "expires_at": time.time() + expires_in,
            "scope": payload.get("scope", ""),
        }
        self.token_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load_token(self) -> dict:
        if not self.token_path.exists():
            return {}
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def access_token(self) -> str:
        """拿一个可用的 access_token（过期就刷）。"""
        record = self._load_token()
        token = str(record.get("access_token") or "")
        if token and float(record.get("expires_at") or 0) > time.time() + 120:
            return token

        refresh = str(record.get("refresh_token") or self.refresh_token or "")
        if not refresh:
            raise BaiduError(
                "没有 refresh_token。先跑 `run.py baidu-login` 完成一次授权"
            )
        if not self.app_key or not self.secret_key:
            raise BaiduError("没配 delivery.baidu.app_key / secret_key，无法续期")

        payload = self._call(
            "POST", self.oauth_token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": self.app_key,
                "client_secret": self.secret_key,
            },
            timeout=30,
        )
        if "access_token" not in payload:
            raise BaiduError(
                f"刷新 access_token 失败：{payload.get('error_description') or payload}"
            )
        payload.setdefault("refresh_token", refresh)
        self._save_token(payload)
        return str(payload["access_token"])

    def user_info(self) -> dict:
        return self._call(
            "GET", self.xpan_nas_url,
            params={"method": "uinfo", "access_token": self.access_token()},
        )

    # ── 上传 ───────────────────────────────────────────────────────
    def _slice_hashes(self, path: Path, slice_bytes: int) -> list[str]:
        hashes: list[str] = []
        with open(path, "rb") as fh:
            while True:
                block = fh.read(slice_bytes)
                if not block:
                    break
                hashes.append(hashlib.md5(block).hexdigest())
        return hashes or [hashlib.md5(b"").hexdigest()]

    def _precreate(self, remote_path: str, size: int, block_list: list[str],
                   token: str) -> tuple[str, list[int], str]:
        payload = self._call(
            "POST", self.xpan_file_url,
            params={"method": "precreate", "access_token": token},
            data={
                "path": remote_path,
                "size": size,
                "isdir": 0,
                "autoinit": 1,
                # rtype=3 覆盖同名文件：文件名里已带订单号，正常不会撞；
                # 用 3 而不是 1，是为了让 create 阶段的 path 保持确定。
                "rtype": 3,
                "block_list": json.dumps(block_list),
            },
            timeout=60,
        )
        self._check(payload.get("errno"), payload, "预上传 precreate")
        uploadid = str(payload.get("uploadid") or "")
        need = [int(x) for x in (payload.get("block_list") or [])]
        return uploadid, need, str(payload.get("path") or remote_path)

    def _pick_upload_host(self, token: str) -> str:
        """试出可用的上传域名。第一个能返回非网络错误的就认它。"""
        if self._upload_host:
            return self._upload_host
        last_error = ""
        for host in self.upload_hosts:
            try:
                # 用一个必然失败的分片请求探路：只要不是连接层错误，
                # 就说明这个域名是通的（业务错误由真正的上传去处理）。
                self._call(
                    "GET", f"{host}/rest/2.0/pcs/superfile2",
                    params={"method": "upload", "access_token": token},
                    timeout=min(self.timeout, 10),
                )
            except BaiduError as exc:
                if "网络错误" in str(exc):
                    last_error = str(exc)
                    continue
                self._upload_host = host
                return host
            else:
                self._upload_host = host
                return host
        raise BaiduError(
            f"没有可用的上传域名（候选：{', '.join(self.upload_hosts)}）。"
            f"最后错误：{last_error}"
        )

    def _upload_slice(self, host: str, remote_path: str, uploadid: str,
                      partseq: int, blob: bytes, token: str) -> None:
        # 这一步的参数**必须全放 query**，只有文件内容进 body。
        # 官方文档写得很明确；把 method / partseq 塞进表单里会静默失败。
        query = urllib.parse.urlencode({
            "method": "upload",
            "access_token": token,
            "type": "tmpfile",
            "path": remote_path,
            "uploadid": uploadid,
            "partseq": partseq,
        })
        url = f"{host}/rest/2.0/pcs/superfile2?{query}"
        resp = http_util.post_multipart(
            url,
            files=[("file", f"blob{partseq}", blob)],
            timeout=max(120.0, self.timeout * 4),
            proxy=self.proxy,
            verify_tls=self.verify_tls,
        )
        if resp.error:
            raise BaiduError(f"分片 {partseq} 上传网络错误：{resp.error}")
        if resp.status >= 400:
            raise BaiduError(f"分片 {partseq} 上传失败：HTTP {resp.status} {resp.text[:160]}")
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise BaiduError(f"分片 {partseq} 响应异常：{resp.text[:160]}") from exc
        # 分片接口的 errno 有时不在正文里，缺了就只看正文有没有 md5
        if "md5" not in payload and int(payload.get("errno") or 0) != 0:
            raise BaiduError(
                f"分片 {partseq} 上传失败：errno={payload.get('errno')} {payload}"
            )

    def _create(self, remote_path: str, size: int, block_list: list[str],
                uploadid: str, token: str) -> str:
        payload = self._call(
            "POST", self.xpan_file_url,
            params={"method": "create", "access_token": token},
            data={
                "path": remote_path,
                "size": size,
                "isdir": 0,
                "rtype": 3,
                "block_list": json.dumps(block_list),
                "uploadid": uploadid,
            },
            timeout=60,
        )
        self._check(payload.get("errno"), payload, "合并文件 create")
        return str(payload.get("fs_id") or "")

    # ── 分享 ───────────────────────────────────────────────────────
    def _share_old(self, fs_id: str, token: str) -> UploadResult:
        """旧版分享接口。

        坑：承载文件 id 的参数名在不同版本的官方文档里叫法不同
        （``fid_list`` / ``fsid_list`` / ``file_id_list``）。**漏掉它会返回
        errno=2（参数错误）**，看着特别像「没有分享权限」，其实只是参数没给。
        所以三个名字依次试，直到有一个被接受。
        """
        problems: list[str] = []
        for key in _SHARE_FID_KEYS:
            payload = self._call(
                "POST", self.xpan_share_url,
                params={"method": "set", "access_token": token},
                data={
                    key: json.dumps([str(fs_id)]),
                    "period": self.share_period_days,
                    "pwd": self._share_pwd(),
                    "schannel": 0,
                    "channel": "chunlei",
                    "clienttype": 0,
                    "web": 1,
                },
                timeout=40,
            )
            errno = int(payload.get("errno") or 0)
            if errno == 0:
                link = str(payload.get("link") or payload.get("shorturl") or "")
                if link:
                    return UploadResult(
                        ok=True, url=link, pwd=str(payload.get("pwd") or ""),
                        fs_id=fs_id, raw=payload,
                    )
                problems.append(f"{key}: 没有返回 link")
                continue
            problems.append(f"{key}: {explain_share_errno(errno)}")
            if errno in _AUTH_ERRORS or errno == 2:
                # errno=2 是「没有分享能力」，换参数名试多少次都一样，别白试
                break
        raise BaiduError("创建分享链接（旧接口）失败 —— " + "；".join(problems))

    def _share_new(self, fs_id: str, token: str) -> UploadResult:
        payload = self._call(
            "POST", self.share_new_url,
            params={
                "product": "netdisk", "appid": self.app_key,
                "access_token": token,
            },
            data={
                "fsid_list": json.dumps([str(fs_id)]),
                "period": self.share_period_days,
                "pwd": self._share_pwd(),
                "schannel": 0,
            },
            timeout=40,
        )
        errno = int(payload.get("errno") or 0)
        if errno != 0:
            # 这个接口不需要试参数名，直接把人话抛出去
            raise BaiduError(
                "创建分享链接（新接口）失败 —— " + explain_share_errno(errno)
                + (f"（{payload.get('show_msg')}）" if payload.get("show_msg") else "")
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        link = str(data.get("link") or data.get("shorturl") or "")
        return UploadResult(
            ok=bool(link), url=link, pwd=str(data.get("pwd") or ""),
            fs_id=fs_id, raw=payload,
            error="" if link else "分享接口没返回链接",
        )

    def _share_pwd(self) -> str:
        return self.share_pwd or random_pwd(4)

    def _share(self, fs_id: str, token: str) -> UploadResult:
        order = ["old", "new"] if self.share_api in ("auto", "") else [self.share_api]
        errors: list[str] = []
        for which in order:
            try:
                result = self._share_old(fs_id, token) if which == "old" \
                    else self._share_new(fs_id, token)
            except BaiduError as exc:
                errors.append(f"{which}: {exc}")
                continue
            if result.ok:
                return result
            errors.append(f"{which}: {result.error}")
        return UploadResult(
            ok=False, fs_id=fs_id,
            error=("创建分享链接都失败了（可能是应用没有分享权限）。"
                   + " | ".join(errors)),
        )

    # ── 对外 ───────────────────────────────────────────────────────
    def upload(self, path: str | Path, remote_name: str = "") -> UploadResult:
        src = Path(path)
        if not src.is_file():
            return UploadResult(ok=False, error=f"文件不存在：{src}")
        try:
            return self._upload(src, remote_name)
        except BaiduError as exc:
            return UploadResult(ok=False, error=str(exc))
        except OSError as exc:
            return UploadResult(ok=False, error=f"读文件失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 上传绝不能炸穿交付流程
            return UploadResult(
                ok=False, error=f"{type(exc).__name__}: {exc}"
            )

    def _upload(self, src: Path, remote_name: str) -> UploadResult:
        if not self.app_key:
            return UploadResult(
                ok=False,
                error="还没配 delivery.baidu.app_key（先去百度网盘开放平台建应用）",
            )
        name = (remote_name or src.name).replace("/", "_").replace("\\", "_")
        remote_path = f"{self.remote_dir}/{name}"
        size = src.stat().st_size
        slice_bytes = self.slice_mb * 1024 * 1024
        token = self.access_token()

        blocks = self._slice_hashes(src, slice_bytes)
        uploadid, need, real_path = self._precreate(remote_path, size, blocks, token)

        if need:
            host = self._pick_upload_host(token)
            with open(src, "rb") as fh:
                for partseq in need:
                    fh.seek(partseq * slice_bytes)
                    blob = fh.read(slice_bytes)
                    self._upload_slice(host, real_path, uploadid, partseq, blob, token)

        fs_id = self._create(real_path, size, blocks, uploadid, token)
        if not fs_id:
            return UploadResult(ok=False, error="合并文件成功但没拿到 fs_id",
                                remote_path=real_path)

        info = {"name": name, "size": size, "slices": len(blocks),
                "uploaded_slices": len(need)}

        if not self.share_enabled:
            # upload_only：文件到网盘就算成功，不碰分享接口
            return UploadResult(ok=True, fs_id=fs_id, remote_path=real_path,
                                raw={**info, "share": "skipped(upload_only)"})

        shared = self._share(fs_id, token)
        shared.remote_path = real_path
        shared.raw = {**shared.raw, **info}
        return shared

    def describe(self) -> str:
        who = "未配置 AppKey"
        if self.app_key:
            who = f"AppKey {self.app_key[:6]}… 目录 {self.remote_dir}"
        return f"{self.label}（{who}）"
