"""本地抓包所需的"TLS 中间人零件"：CA、叶证书、装/卸证书、系统代理开关。

设计原则（都是为了"静默"且不伤用户机器）：

1. **只解视频号相关域名**（见 :data:`xydl.wxproxy.MITM_HOSTS`），其余域名一律原样
   隧道转发 —— 不做全量中间人。
2. **证书装进「当前用户」的根证书库**（``certutil -user``），**不需要管理员权限**，
   也不污染机器级证书库；卸载同样一条命令。
3. **链式上游代理**：用户本来可能开着 Clash/v2ray（本机实测是 ``127.0.0.1:7897``）。
   我们改系统代理时把上游记下来，非目标域名的流量转发给它，退出时**原样还原**，
   否则一跑工具就把用户网断了。
4. 证书用 **openssl CLI** 生成（Git for Windows 自带，实测 3.5.5）；找不到 openssl
   时退化到 ``cryptography``（可选依赖）；两者都没有就给一句人话报错。
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

#: CA 证书的 CN，用于在证书库里识别/卸载
CA_NAME = "XYDL Video Capture CA"

_OPENSSL_CANDIDATES = (
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
)


def find_openssl() -> str:
    """找可用的 openssl。找不到返回空串。"""
    for path in _OPENSSL_CANDIDATES:
        if os.path.exists(path):
            return path
    return shutil.which("openssl") or ""


def _run(cmd: list[str], *, timeout: float = 60.0) -> tuple[int, str]:
    """跑一个外部命令。返回 ``(返回码, 输出)``；找不到命令返回 ``(-1, ...)``。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        return -1, f"找不到命令：{cmd[0]}"
    except subprocess.SubprocessError as exc:
        return -1, f"命令失败：{exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


@dataclass
class CertAuthority:
    """自签 CA + 按域名签叶证书（全部落在 ``dir`` 里，可整体删除）。"""

    dir: Path

    def __post_init__(self) -> None:
        # 微信对同一域名会**并发**开一堆连接，多个线程同时给同一个 host 签叶证书
        # 时会互相覆盖 key 文件，结果证书和私钥对不上：
        #   ssl.SSLError: [X509: KEY_VALUES_MISMATCH] key values mismatch
        # 实测就是这样：流量进来了、一个响应都没截到，日志刷满握手失败。
        # 所以整个签发过程必须串行化。
        self._lock = threading.RLock()

    @property
    def ca_crt(self) -> Path:
        return self.dir / "ca.crt"

    @property
    def ca_key(self) -> Path:
        return self.dir / "ca.key"

    @property
    def ca_pem(self) -> Path:
        return self.dir / "ca.pem"

    # ── CA ─────────────────────────────────────────────────────────
    def ensure(self) -> tuple[bool, str]:
        """确保 CA 存在。返回 ``(是否可用, 说明)``。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.ca_crt.exists() and self.ca_key.exists():
            return True, "CA 已存在"

        openssl = find_openssl()
        if openssl:
            code, out = _run([
                openssl, "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                "-keyout", str(self.ca_key), "-out", str(self.ca_crt),
                "-days", "3650", "-subj", f"/CN={CA_NAME}",
                "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            ])
            if code != 0:
                return False, f"openssl 生成 CA 失败：{out[-300:]}"
            self._to_pem()
            return True, f"已用 openssl 生成 CA（{self.ca_crt}）"

        ok, msg = self._ensure_with_cryptography()
        return ok, msg

    def _to_pem(self) -> None:
        """把 CA 证书另存一份 .pem（有些客户端只认 pem 扩展名）。"""
        try:
            self.ca_pem.write_bytes(self.ca_crt.read_bytes())
        except OSError:
            pass

    def _ensure_with_cryptography(self) -> tuple[bool, str]:
        import importlib.util

        if importlib.util.find_spec("cryptography") is None:
            return False, ("生成证书需要 openssl 或 cryptography："
                           "装 Git for Windows（自带 openssl）或 "
                           "pip install cryptography")
        # 交给 cryptography 的完整实现（在打包 exe 里没有 openssl 时用）
        try:
            from ._mitm_crypto import generate_ca
        except ImportError:
            return False, "cryptography 后端不可用（缺少 xydl/_mitm_crypto.py）"
        return generate_ca(self.ca_crt, self.ca_key)

    # ── 叶证书 ─────────────────────────────────────────────────────
    def leaf_for(self, host: str) -> tuple[Path, Path] | tuple[None, None]:
        """给某个域名签一张叶证书（带缓存）。返回 ``(cert, key)``。

        全程持锁：并发请求同一个 host 时必须只有一个线程在签发，
        否则 cert/key 会互相覆盖成不配对的组合（实测踩过）。
        """
        with self._lock:
            safe = host.replace(":", "_").replace("*", "_")
            cert_path = self.dir / f"leaf-{safe}.crt"
            key_path = self.dir / f"leaf-{safe}.key"
            if cert_path.exists() and key_path.exists():
                if self._pair_ok(cert_path, key_path):
                    return cert_path, key_path
                # 已有的这配对坏了（被并发写坏/CA 换过）→ 删掉重签
                for bad in (cert_path, key_path):
                    try:
                        bad.unlink()
                    except OSError:
                        pass
            return self._sign_leaf(host, cert_path, key_path, safe)

    @staticmethod
    def _pair_ok(cert_path: Path, key_path: Path) -> bool:
        """证书和私钥是不是配对的（拿 ssl 自己验一遍最可靠）。"""
        try:
            probe = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            probe.load_cert_chain(str(cert_path), str(key_path))
            return True
        except (ssl.SSLError, OSError, ValueError):
            return False

    def _sign_leaf(self, host: str, cert_path: Path, key_path: Path,
                   safe: str) -> tuple[Path, Path] | tuple[None, None]:
        openssl = find_openssl()
        if not openssl:
            return None, None
        # 主机名可能是 IP（测试用），SAN 里要写 IP: 而不是 DNS:
        if host.replace(".", "").isdigit():
            san = f"IP:{host}"
        else:
            san = f"DNS:{host}"
        # 先生成到**临时文件名**再原子改名：就算别的进程/线程在看这两个文件，
        # 也不会看到"新证书 + 旧私钥"这种半截状态。
        tmp_key = self.dir / f".tmp-{safe}.key"
        tmp_cert = self.dir / f".tmp-{safe}.crt"
        csr = self.dir / f".tmp-{safe}.csr"
        ext = self.dir / f".tmp-{safe}.ext"
        ext.write_text(
            "basicConstraints=CA:FALSE\n"
            "keyUsage=digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            f"subjectAltName={san}\n",
            encoding="utf-8",
        )
        try:
            code, out = _run([openssl, "req", "-nodes", "-newkey", "rsa:2048",
                              "-keyout", str(tmp_key), "-out", str(csr),
                              "-subj", f"/CN={host}"])
            if code != 0:
                return None, None
            code, out = _run([openssl, "x509", "-req", "-in", str(csr),
                              "-CA", str(self.ca_crt), "-CAkey", str(self.ca_key),
                              "-CAcreateserial", "-out", str(tmp_cert),
                              "-days", "825", "-extfile", str(ext)])
            if code != 0:
                return None, None
            # 签完先自检配对，再原子改名
            if not self._pair_ok(tmp_cert, tmp_key):
                return None, None
            os.replace(tmp_key, key_path)
            os.replace(tmp_cert, cert_path)
        finally:
            for tmp in (csr, ext, tmp_key, tmp_cert):
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return cert_path, key_path


# ══════════════════════════════════════════════════════════════════════
#  证书库装卸（Windows；用 certutil -user，不需要管理员）
# ══════════════════════════════════════════════════════════════════════
def install_ca(ca_crt: Path, name: str = CA_NAME) -> tuple[bool, str]:
    """把 CA 装进**当前用户**的「受信任的根证书颁发机构」。

    超时给得短（20s）：``certutil`` 偶尔会卡很久（证书库大 / 系统在弹安全确认），
    卡住时**必须让启动流程继续**，不能把整个抓包服务堵在门口（实测踩过：
    服务打印完"CA 已存在"就没下文了，端口一直不监听）。
    """
    if sys.platform != "win32":
        return False, "自动装证书目前只实现了 Windows（其他平台请手动信任 ca.crt）"
    code, out = _run(["certutil", "-user", "-addstore", "-f", "Root", str(ca_crt)],
                     timeout=20.0)
    if code != 0:
        return False, (f"certutil 装证书失败或超时（{out[-200:]}）"
                       "—— 服务照常启动；只是不认这张证书的客户端（如微信原生栈）没法解密")
    return True, f"已把「{name}」装进当前用户根证书库（不需要管理员权限）"


def uninstall_ca(name: str = CA_NAME) -> tuple[bool, str]:
    code, out = _run(["certutil", "-user", "-delstore", "Root", name], timeout=20.0)
    if code != 0:
        return False, f"certutil 卸载失败（可能本来就没装）：{out[-200:]}"
    return True, f"已从当前用户根证书库移除「{name}」"


def ca_installed(name: str = CA_NAME) -> bool:
    if sys.platform != "win32":
        return False
    code, out = _run(["certutil", "-user", "-store", "Root", name], timeout=20.0)
    return code == 0 and name.split()[0] in out


# ══════════════════════════════════════════════════════════════════════
#  系统代理开关（HKCU，可还原）
# ══════════════════════════════════════════════════════════════════════
_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


@dataclass
class ProxyState:
    """改系统代理前记下来的原始状态。"""

    enable: int = 0
    server: str = ""
    bypass: str = ""

    def as_dict(self) -> dict:
        return {"enable": self.enable, "server": self.server, "bypass": self.bypass}


class SystemProxy:
    """读写 Windows 的系统代理（WinINET / HKCU），支持完整还原。

    为什么必须还原：用户机器上很可能本来就开着 Clash/v2ray（本机实测是
    ``127.0.0.1:7897``）。改了不还原 = 一跑完工具就没网，这是最容易被骂的 bug。
    """

    def __init__(self, state_file: Path | None = None):
        self.state_file = Path(state_file) if state_file else None
        self.previous: ProxyState | None = None

    # ── 读写注册表 ─────────────────────────────────────────────────
    @staticmethod
    def _read() -> ProxyState:
        if sys.platform != "win32":
            return ProxyState()
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH) as key:
                def get(name, default):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                        return value
                    except OSError:
                        return default

                return ProxyState(
                    enable=int(get("ProxyEnable", 0) or 0),
                    server=str(get("ProxyServer", "") or ""),
                    bypass=str(get("ProxyBypass", "") or ""),
                )
        except OSError:
            return ProxyState()

    @staticmethod
    def _write(enable: int, server: str | None = None,
               bypass: str | None = None) -> None:
        if sys.platform != "win32":
            return
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, enable)
            if server is not None:
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
            if bypass is not None:
                winreg.SetValueEx(key, "ProxyBypass", 0, winreg.REG_SZ, bypass)

    # ── 对外 ───────────────────────────────────────────────────────
    def snapshot(self) -> ProxyState:
        self.previous = self._read()
        if self.state_file:
            try:
                import json

                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(
                    json.dumps(self.previous.as_dict(), ensure_ascii=False),
                    encoding="utf-8")
            except OSError:
                pass
        return self.previous

    def set_to(self, host: str, port: int) -> None:
        """把系统代理指向本地抓包端口（先 snapshot 过）。"""
        self._write(1, f"{host}:{port}")

    def restore(self) -> str:
        """还原到改动前的状态。返回一句人话说明。"""
        prev = self.previous
        if prev is None and self.state_file and self.state_file.exists():
            try:
                import json

                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                prev = ProxyState(int(data.get("enable", 0)),
                                  str(data.get("server", "")),
                                  str(data.get("bypass", "")))
            except (OSError, ValueError):
                prev = None
        if prev is None:
            return "没有记录到原始代理状态，未做还原"
        self._write(prev.enable, prev.server or None, prev.bypass or None)
        if self.state_file:
            try:
                self.state_file.unlink()
            except OSError:
                pass
        if prev.enable and prev.server:
            return f"已还原系统代理为 {prev.server}"
        return "已关闭系统代理（原来没开）"

    @staticmethod
    def leaked(port: int, host: str = "127.0.0.1") -> bool:
        """系统代理是不是还指着我们（上次被强杀留下的残局）。

        进程被强杀时 ``finally`` 不会执行，用户就会"没网"。所以启动时先自检、
        发现残留立刻还原 —— 不能指望退出路径一定跑到。
        """
        current = SystemProxy._read()
        return bool(current.enable) and f"{host}:{port}" in (current.server or "")

    def describe(self) -> str:
        cur = self._read()
        if cur.enable and cur.server:
            return f"系统代理：{cur.server}"
        return "系统代理：未启用"


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    """等端口真的起来（改系统代理前必须确认，否则用户会短暂没网）。"""
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False
