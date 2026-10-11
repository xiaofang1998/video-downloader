"""本地抓包：只对视频号相关的域名做 TLS 中间人，把媒体地址与 decode_key 截下来。

为什么要有这个模块（以及为什么不是"装第三方工具"）：
微信视频号在浏览器里不播（预览页只给封面+二维码），匿名接口也不给媒体字段，
所以本机唯一可行的办法就是**让微信 PC 客户端去播，然后在网络层把它截下来**。
社区做法（ltaoo/wx_channels_download 等）都是本机代理 + 根证书；这里把它做进项目
自己，用户只面对"粘链接"一个动作。

工程上的三条自我约束：

* **选择性中间人**：只有 :data:`MITM_HOSTS` 里的域名会被解密，其余一律原样隧道
  转发（含 CONNECT 到非目标域名）。不做全量中间人。
* **链式上游代理**：用户机器上可能本来就开着 Clash（本机实测 ``127.0.0.1:7897``），
  所有非目标流量都转给它，退出时还原系统代理（见 :mod:`xydl.mitm`）。
* **只认视频地址**：封面的 ``stodownload`` 和视频走同一个接口，必须靠
  :func:`xydl.wechat_channels.looks_like_video_url` 区分，否则会把封面 JPEG
  当视频交付（这条坑已经踩过一次）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import select
import socket
import ssl
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .wechat_channels import looks_like_video_url

#: 需要中间人解密的域名（其余一律原样转发）
MITM_HOSTS = (
    "channels.weixin.qq.com",     # 视频号页面 + finderassistant 接口（媒体地址与 key 在这里）
    "finder.video.qq.com",        # 视频 CDN
    "wxapp.tc.qq.com",            # 另一种 CDN 域名
    # 视频号页面的**静态资源**在这里（实测页面 HTML 里 3 个资源全是 res.wx.qq.com）。
    # 不放进白名单的话只能往 HTML 注内联脚本 —— 那条路被 CSP 拦，脚本根本不执行。
    "res.wx.qq.com",
)

#: 只解这些域名里的"媒体/CDN"路径，避免把整站响应都留在磁盘上
_INTERESTING_PATH = re.compile(
    r"/cgi-bin/|/finder|/sph|stodownload|get_media|/web/pages", re.I)

_UNSAFE_PRIVACY_KEYS = ("cookie", "authorization", "token")


# ══════════════════════════════════════════════════════════════════════
#  从 JSON 里挖媒体地址 + decode_key
# ══════════════════════════════════════════════════════════════════════
@dataclass
class MediaHit:
    url: str
    decode_key: str = ""
    title: str = ""
    object_id: str = ""

    def as_entry(self, source: str) -> dict:
        data = asdict(self)
        data["source"] = source
        data["ts"] = time.time()
        return data


_URL_KEYS = ("url", "videoUrl", "video_url", "download_url", "downloadUrl",
             "full_url", "fullUrl", "src", "play_url", "playUrl")
_KEY_KEYS = ("decode_key", "decodeKey", "decode_key_str", "decodeKeyStr")
_DESC_KEYS = ("description", "title", "desc", "objectDesc", "object_desc")
_ID_KEYS = ("object_id", "objectId", "export_id", "exportId", "id")


def _first_str(node: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and key in _KEY_KEYS:
            return str(int(value))          # decode_key 有时是数字
    return ""


def extract_media(payload: Any) -> list[MediaHit]:
    """递归找出所有「视频地址（+ 可能的 decode_key）」。

    做成通用递归而不是写死字段路径：视频号的接口结构随版本变（不同接口返回
    ``data.object_desc.media[0]``，网页接口又是另一套），写死必然很快失效。
    ``decode_key`` 常在父层，所以要"继承"着往下传。
    """
    hits: list[MediaHit] = []

    def walk(node: Any, key: str, desc: str, obj: str) -> None:
        if isinstance(node, dict):
            new_key = _first_str(node, _KEY_KEYS) or key
            new_desc = _first_str(node, _DESC_KEYS) or desc
            new_obj = _first_str(node, _ID_KEYS) or obj
            for name in _URL_KEYS:
                value = node.get(name)
                if isinstance(value, str) and looks_like_video_url(value):
                    hits.append(MediaHit(url=value, decode_key=new_key,
                                         title=new_desc, object_id=new_obj))
            for value in node.values():
                walk(value, new_key, new_desc, new_obj)
        elif isinstance(node, list):
            for value in node:
                walk(value, key, desc, obj)

    walk(payload, "", "", "")
    # 同一个 url 去重，优先保留带 key 的那条
    best: dict[str, MediaHit] = {}
    for hit in hits:
        old = best.get(hit.url)
        if old is None or (not old.decode_key and hit.decode_key):
            best[hit.url] = hit
    return list(best.values())


# ══════════════════════════════════════════════════════════════════════
#  抓到的结果存这儿，流水线从这里取
# ══════════════════════════════════════════════════════════════════════
@dataclass
class CaptureStore:
    """抓到的视频地址 + decode_key。线程安全，落盘可跨进程/重启使用。"""

    path: Path | None = None
    max_items: int = 200
    _items: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.load()

    # ── 读写 ───────────────────────────────────────────────────────
    def load(self) -> None:
        if self.path is None or not Path(self.path).exists():
            return
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, list):
            with self._lock:
                self._items = [d for d in data if isinstance(d, dict) and d.get("url")]

    def save(self) -> None:
        if self.path is None:
            return
        try:
            path = Path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._items[-self.max_items:], ensure_ascii=False,
                                      indent=1), encoding="utf-8")
            tmp.replace(path)          # 原子替换，别留半截文件
        except OSError:
            pass

    def add(self, entry: dict) -> bool:
        """加一条。返回是否是新记录（按 url 去重）。"""
        url = str(entry.get("url") or "")
        if not url or not looks_like_video_url(url):
            return False
        with self._lock:
            for old in self._items:
                if old.get("url") == url:
                    # 补上后到的信息（媒体请求先来、API 响应后到是常态；
                    # spec 的档位信息也可能是后到的，要能升级）
                    changed = False
                    if entry.get("decode_key") and not old.get("decode_key"):
                        old["decode_key"] = entry["decode_key"]
                        changed = True
                    for key in ("height", "label"):
                        if entry.get(key) and not old.get(key):
                            old[key] = entry[key]
                            changed = True
                    if entry.get("source") == "spec" and old.get("source") != "spec":
                        old["source"] = "spec"
                        changed = True
                    if changed:
                        old["ts"] = entry.get("ts", time.time())
                        self.save()
                        return True
                    return False
            self._items.append(entry)
            del self._items[:-self.max_items]
        self.save()
        return True

    # ── 查询 ───────────────────────────────────────────────────────
    def entries(self) -> list[dict]:
        with self._lock:
            return list(self._items)

    def with_key(self) -> list[dict]:
        return [e for e in self.entries() if e.get("decode_key")]

    def latest(self, *, max_age: float = 1800.0, need_key: bool = True) -> dict | None:
        """最近一条（默认要求带 decode_key，且不太旧）。

        ``decode_key`` 与直链**必须来自同一次响应**：key 每次请求都会变，
        所以过期的条目直接不认，宁可让用户再播一次。
        """
        now = time.time()
        pool = self.with_key() if need_key else self.entries()
        fresh = [e for e in pool if now - float(e.get("ts") or 0) <= max_age]
        return fresh[-1] if fresh else None

    def find(self, query: str, *, max_age: float = 1800.0) -> dict | None:
        """按标题/object_id 模糊找（多剧集场景：一部剧要多集）。"""
        text = (query or "").strip().lower()
        if not text:
            return None
        now = time.time()
        for entry in reversed(self.with_key()):
            if now - float(entry.get("ts") or 0) > max_age:
                continue
            haystack = " ".join(str(entry.get(k) or "") for k in
                                ("title", "object_id")).lower()
            if text and text in haystack:
                return entry
        return None

    def best_spec(self, quality: str = "", *, max_age: float = 1800.0
                  ) -> dict | None:
        """抓到的**全档位**里按偏好挑一条（只有抓到 spec[] 才有）。

        这是"分辨率可选"的唯一来源：纯 HTTP 那条路服务端只给一档。
        要 1080 就挑高度 ≥1080 里最矮的（最省的满足档），没有就挑最高的。
        """
        now = time.time()
        pool = [e for e in self.entries()
                if e.get("source") == "spec"
                and now - float(e.get("ts") or 0) <= max_age
                and str(e.get("height") or "").isdigit()]
        if not pool:
            return None
        want = str(quality or "").strip()
        if want.isdigit():
            target = int(want)
            ok = [e for e in pool if int(e["height"]) >= target]
            if ok:
                return min(ok, key=lambda e: int(e["height"]))
            return max(pool, key=lambda e: int(e["height"]))
        # 没指定分辨率：取最高的
        return max(pool, key=lambda e: int(e["height"]))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
        self.save()


# ══════════════════════════════════════════════════════════════════════
#  极简 HTTP 报文读写（stdib 里没有可直接复用的，只能自己来）
# ══════════════════════════════════════════════════════════════════════
def capture_store_path(config: Any = None) -> Path:
    """抓包结果的落盘位置（抓包服务与流水线必须指向同一个文件）。"""
    if config is not None:
        try:
            configured = str(
                config.get("download.wechat_channels.capture_store", "") or "")
            if configured:
                return Path(configured).expanduser()
            return Path(config.path_of("data_dir")) / "wx_capture.json"
        except Exception:
            pass
    return Path("data") / "wx_capture.json"


def extract_specs(payload: Any) -> list[dict]:
    """从任意 JSON 里挖出所有**清晰度档位**（``spec[]``）。

    为什么单独做这一步：纯 HTTP（元宝+token）那条路服务端**只给一档**，
    URL 的签名又是和档位绑定的（改 ``X-snsvideoflag`` 没用，实测 400/仍 720p）。
    全档位只存在于**微信客户端自己的接口响应**里：``objectDesc.media[0].spec[]``,
    每一项是一组"为该档位签好名的 URL"。

    所以抓包时要把这些 spec 也存下来 —— 抓到了，清晰度才真的可选。
    """
    hits: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            specs = node.get("spec")
            if isinstance(specs, list):
                for item in specs:
                    if not isinstance(item, dict):
                        continue
                    url = (item.get("url") or item.get("full_url")
                           or item.get("videoUrl") or "")
                    if not isinstance(url, str) or not url:
                        continue
                    height = str(item.get("height") or item.get("videoHeight")
                                 or item.get("videoHeightPx") or "")
                    label = str(item.get("fileFormat") or item.get("spec")
                                or item.get("label") or item.get("definition")
                                or item.get("name") or "")
                    hits.append({
                        "url": url,
                        "height": height,
                        "label": label,
                        "decode_key": str(item.get("decodeKey")
                                          or item.get("decode_key") or ""),
                    })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    seen: set[str] = set()
    out: list[dict] = []
    for hit in hits:
        if hit["url"] in seen:
            continue
        seen.add(hit["url"])
        out.append(hit)
    return out


#: 注入到视频号页面里的脚本：把 `decodeKey` / `spec[]` 抓出来发回本地。
#:
#: 为什么必须注入：微信 4.x 的视频号页是 CEF（Chromium），**MITM 已经能解它的
#: HTML 了**（实测抓到过页面响应），但播放数据是原生侧通过 `WeixinJSBridge`
#: 递给页面的（不走 HTTP），所以 MITM 只看到 CDN、看不到 API 数据 ——
#: `decode_key` 就是这么漏掉的。既然能改 HTML，就往页面里塞脚本，
#: 从页面自己的对象/桥里把 `media[0].decodeKey` 和 `spec[]` 读出来发回本地。
#: 这是我们拿 `decode_key` 的唯一可行路径。
#: 探测用的顶层标识符候选（压缩后的短名 + 常见形态）。
#: ⚠️ **不能含 JS 保留字**：`typeof default!=='undefined'` 是 **SyntaxError**，
#: 会让整段注入代码解析失败、静默不执行（实测踩过：注入成功却零回报）。
_INJECT_NAMES = ("a b c d e f g h i j k l m n o p q r s t u v w x y z "
                 "A B C D E F G H I J K L M N O P Q R S T U V W X Y Z "
                 "_p _t _e _n __p __t p_ t_ e_ n_ module Module exports").split()


def _inject_js_source() -> str:
    """生成**只读**注入代码：探测同文件作用域里的顶层变量，找到解密模块。

    为什么这么写（两轮把用户微信页面弄坏的教训）：
    * **绝不 defineProperty / 绝不包装任何运行时属性**（`self.onmessage`、
      `MessagePort.prototype` 都改坏过页面）—— 本段代码只**读**。
    * 我们的代码是追加在 `worker_release.js` / `wasm_video_decode.js` **同一个文件**
      里的，所以那些压缩后的顶层变量（`p`、`t`…）在闭包里**看得见**：
      用 `typeof x!=='undefined'` 逐个探测，未声明的只会抛 ReferenceError 并被吞掉。
    * 探测到模块对象后：上报它的键；如果有 `decryptor_array`（已生成好的
      128 KiB 密钥流），把密钥流 base64 回传 —— 有密钥流就**不需要密钥**了。
    """
    probes = "\n".join(
        f"  chk({name!r},typeof {name}!=='undefined'?{name}:null);"
        for name in _INJECT_NAMES)
    return """;(function(){
'use strict';
var SELF=(typeof self!=='undefined')?self:this;
// ⚠️ 不能"每 realm 只跑一次就 return"：同一个 blob 里我们被注入到
// **两个**文件（wasm_video_decode.js 末尾 + worker_release.js 的闭包内），
// 而 worker_release.js 那份才是**唯一**能看到 `p.decryptor_array` 的
// （p 在它自己的 IIFE 里）。之前 `if(SELF.__xydlRead)return;` 让后到的那份
// 直接退出 → 永远看不到 p（连白跑好几轮的真正原因）。
// 现在：每个注入点都至少跑一次 probe；只有第一个注入点负责装定时器。
var FIRST=!SELF.__xydlRead;
SELF.__xydlRead=1;
var REPORT='https://channels.weixin.qq.com/__xydl_report';
var HERE=String((SELF.location&&SELF.location.href)||'').slice(0,200);
function send(o){
  try{SELF.fetch(REPORT,{method:'POST',headers:{'Content-Type':'text/plain'},
    body:JSON.stringify(o),keepalive:true});}catch(e){}
}
function b64(u8){
  var CH='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/',s='';
  for(var i=0;i<u8.length;i+=3){
    var a=u8[i],b=u8[i+1],c=u8[i+2];
    s+=CH[a>>2]+CH[((a&3)<<4)|((b||0)>>4)]
      +(i+1<u8.length?CH[(((b||0)&15)<<2)|((c||0)>>6)]:'=')
      +(i+2<u8.length?CH[(c||0)&63]:'=');
  }
  return s;
}
var reported=false;
var found=[];
function chk(name,v){
  try{
    if(!v||typeof v!=='object')return;
    // 无条件记录"这个作用域里有什么"（截前 14 个），供第一轮上报定性：
    // 沉默是歧义的 —— 分不清"代码没跑"还是"跑了但没找到目标"。
    if(found.length<14){
      var ks0=[];try{ks0=Object.keys(v).slice(0,14);}catch(e){}
      found.push(name+'{'+ks0.join(',')+'}');
    }
    if(reported)return;
    var ks=null,keys=null;
    try{keys=Object.keys(v);}catch(e){keys=[];}
    var hasKs=keys.indexOf('decryptor_array')>=0;
    if(hasKs){
      var arr=v.decryptor_array;
      var u8=arr&&arr.buffer?new Uint8Array(arr.buffer,arr.byteOffset||0,arr.byteLength)
                            :(arr instanceof Uint8Array?arr:null);
      if(u8&&u8.length>0){ks=u8;}
    }
    // **只有** `decryptor_array`（密钥流）才算找到，别的一个都不认。
    // 踩过两次：worker 全局对象上启动时就挂着 `onmessage` / `initDecrtptor`，
    // 拿它们当判据会立刻 reported=true 停掉轮询，而密钥流是**播放之后**
    // 才生成的 → 永远拿不到。
    var interesting=hasKs;
    if(!interesting)return;
    reported=true;
    var msg={tag:'module',name:name,here:HERE,keys:keys.slice(0,40),
             hasKeystream:!!ks};
    if(ks){
      msg.ksLen=ks.length;
      msg.ksB64=b64(ks.slice(0,131072));      // 只发前 128 KiB（就是解密要的前缀）
    }
    send(msg);
  }catch(e){}
}
function probe(){
  try{
%s
  }catch(e){}
}
if(typeof window!=='undefined'){
  // 页面上下文：只报一次"我在"，不做任何其它事
  if(location.hostname==='channels.weixin.qq.com')send({tag:'alive',here:HERE});
}
var n=0,lastFound=0;
var timer=null;
if(FIRST){
  timer=setInterval(function(){
    n++;probe();
    // 只要**又看到新对象**就再报一次。原因：同一个 blob 里我们的代码排在
    // wasm_video_decode.js 末尾，而 worker_release.js（`p.decryptor_array` 所在）
    // 是**之后**才拼接进来的 —— 第一轮探测时 p 还没定义（实测只看到 Module）。
    if(found.length>lastFound){
      lastFound=found.length;
      try{send({tag:'probe',round:n,here:HERE,found:found});}catch(e){}
    }
    if(n>=24||reported){clearInterval(timer);
      if(reported)send({tag:'module-done',here:HERE,tries:n});}
  },1500);
}
probe();
if(found.length>lastFound){lastFound=found.length;
  try{send({tag:'probe',round:FIRST?0:-1,here:HERE,found:found});}catch(e){}}
})();""" % probes


#: 只读注入代码（只算一次；内容与设计约束见 _inject_js_source 的说明）
_INJECT_JS = _inject_js_source()


def _inject_js(body: bytes) -> bytes:
    """把抓取代码插进页面自己的 JS 文件。

    **插在哪儿是关键**（踩了很多轮才想通）：
    `worker_release.js` 整个文件是一个箭头 IIFE：`(()=>{ … })();`，
    里面所有变量（包括我们要读的 `p.decryptor_array`）都在闭包内部。
    如果简单追加到文件**末尾**，我们就在闭包**外面** → `typeof p==='undefined'`
    → 探测一个都找不到 → **零回报**（现象就是"注入成功却什么都读不到"）。
    所以：检测到文件是 IIFE 时，把代码插到结尾 `})();` **之前**，
    让它落在闭包内部；否则（如 emscripten 的 `wasm_video_decode.js`，
    顶层就是 `var Module`）追加在末尾即可。
    """
    text = body.decode("utf-8", "replace")
    tail_zone = max(0, len(text) - 240)
    for tail in ("})();", "}());", "})()", "}())"):
        idx = text.rfind(tail)
        if idx > tail_zone:
            return (text[:idx] + "\n" + _INJECT_JS + "\n" + text[idx:]).encode("utf-8")
    return (text + "\n" + _INJECT_JS).encode("utf-8")


_INJECT_SCRIPT = "<script>" + _INJECT_JS + "</script>"


def _inject_html(body: bytes) -> bytes:
    """把注入脚本插进 HTML（尽量插在 <head> 之后，保证尽早执行）。"""
    text = body.decode("utf-8", "replace")
    lower = text.lower()
    for anchor in ("<head>", "<head ", "<html"):
        idx = lower.find(anchor)
        if idx >= 0:
            end = text.find(">", idx) + 1
            return (text[:end] + _INJECT_SCRIPT + text[end:]).encode("utf-8")
    return (_INJECT_SCRIPT + text).encode("utf-8")


#: 注入脚本把数据回报到**页面自己的域名**下这个路径。
#: 同源 -> 不受 CSP connect-src/CORS/混合内容限制；代理在转发前就把它拦下回 200。
REPORT_PATH = "/__xydl_report"


def _safe_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}


def _read_head(stream) -> tuple[str, dict[str, str]]:
    """读到空行为止，返回 ``(起始行, 头)``。"""
    line = stream.readline()
    if not line:
        raise ConnectionError("连接已关闭")
    start = line.decode("latin-1").strip()
    headers: dict[str, str] = {}
    while True:
        raw = stream.readline()
        if not raw or raw in (b"\r\n", b"\n"):
            break
        text = raw.decode("latin-1").rstrip("\r\n")
        if ":" in text:
            name, _, value = text.partition(":")
            headers[name.strip().lower()] = value.strip()
    return start, headers


def _read_chunked(stream) -> bytes:
    out = bytearray()
    while True:
        line = stream.readline()
        if not line:
            break
        size_text = line.split(b";")[0].strip()
        try:
            size = int(size_text or b"0", 16)
        except ValueError:
            break
        if size == 0:
            # 吃掉 trailer
            while True:
                trailer = stream.readline()
                if not trailer or trailer in (b"\r\n", b"\n"):
                    break
            break
        out += stream.read(size)
        stream.readline()
    return bytes(out)


def _read_body(stream, headers: dict[str, str]) -> bytes:
    if "chunked" in headers.get("transfer-encoding", "").lower():
        return _read_chunked(stream)
    length = headers.get("content-length")
    if length and length.isdigit():
        return stream.read(int(length))
    return b""


def _forward_request_headers(headers: dict[str, str], host: str) -> dict[str, str]:
    """改写请求头再转发。

    关键一条：**删掉 Accept-Encoding**。上游就不会压缩，我们才能直接解析 JSON；
    否则还要引入 brotli/zstd 才能解压（stdlib 只有 gzip/deflate）。
    """
    out = {}
    for name, value in headers.items():
        if name in ("accept-encoding", "proxy-connection", "connection",
                    "keep-alive", "te", "upgrade"):
            continue
        out[name] = value
    out["connection"] = "close"
    return out


def _build_request(method: str, target: str, headers: dict[str, str],
                   body: bytes) -> bytes:
    lines = [f"{method} {target} HTTP/1.1"]
    for name, value in headers.items():
        lines.append(f"{name}: {value}")
    if body and "content-length" not in headers:
        lines.append(f"content-length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def _build_response(status: int, reason: str, headers: dict[str, str],
                    body: bytes) -> bytes:
    lines = [f"HTTP/1.1 {status} {reason}".rstrip()]
    for name, value in headers.items():
        if name in ("transfer-encoding", "content-length", "connection"):
            continue
        lines.append(f"{name}: {value}")
    lines.append(f"content-length: {len(body)}")
    lines.append("connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


# ══════════════════════════════════════════════════════════════════════
#  代理本体
# ══════════════════════════════════════════════════════════════════════
class ChannelsProxy:
    """只对视频号域名做中间人的本地代理。

    ``upstream`` 是链式上游（用户原本的 Clash 之类）。目标域名我们自己直连；
    其余流量原样转给上游，保证用户"开着工具也能正常上网"。
    """

    def __init__(self, store: CaptureStore, *, host: str = "127.0.0.1",
                 port: int = 2023, ca=None, mitm_hosts: tuple[str, ...] = MITM_HOSTS,
                 upstream: str = "", verify_upstream: bool = True,
                 upstream_map: dict[str, tuple[str, int]] | None = None,
                 log: Callable[[str], None] | None = None):
        self.store = store
        #: 是否往视频号页面注入抓取代码（拿 decodeKey/spec 的唯一可行路径）。
        #: **默认关**：注入会改写页面自己的 HTML/JS，实测会把用户的视频号页面
        #: 弄成打不开（CSP 摘头 + 往 res.wx.qq.com 的公共 bundle 里塞代码，
        #: 后果波及面比预期大）。所以要显式 `XYDL_CAPTURE_INJECT=1` 才开，
        #: 而且只在你自己机器上试的时候开。
        self.inject = os.environ.get("XYDL_CAPTURE_INJECT", "0").strip() not in ("", "0")
        #: 诊断开关：把每个解密到的请求/响应都记日志，较大的响应体落盘。
        #: 只在"打开抓包但零捕获"时开 —— 用它看微信客户端真实的报文形态。
        self.debug = os.environ.get("XYDL_CAPTURE_DEBUG", "").strip() not in ("", "0")
        self.debug_dir: Path | None = None
        if self.debug:
            self.debug_dir = Path(os.environ.get("XYDL_CAPTURE_DUMP", "data/capture_dump"))
            try:
                self.debug_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.debug_dir = None
        self.host = host
        self.port = int(port)
        self.ca = ca
        self.mitm_hosts = tuple(h.lower() for h in mitm_hosts)
        self.upstream = (upstream or "").strip()
        self.verify_upstream = verify_upstream
        #: 测试钩子：把某个域名指到本地地址，就能用**真实域名**跑通整条链路
        self.upstream_map = dict(upstream_map or {})
        self.log = log or (lambda _m: None)
        self.stats = {"connect": 0, "mitm": 0, "tunnel": 0, "captured": 0,
                      "responses": 0}
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._ssl_cache: dict[str, ssl.SSLContext] = {}
        self._cache_lock = threading.Lock()

    # ── 生命周期 ───────────────────────────────────────────────────
    def start(self) -> None:
        proxy = self

        class Handler(_ProxyHandler):
            pass

        Handler.proxy = proxy                      # 绑给 handler 用
        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        # server_address 里的端口是真实端口：port 传 0 时由系统分配（测试/自动选端口用）
        self.port = int(self._httpd.server_address[1])
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.2}, daemon=True)
        self._thread.start()
        self.log(f"抓包代理已启动：{self.host}:{self.port}"
                 + (f"（上游 {self.upstream}）" if self.upstream else ""))

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self.log("抓包代理已停止")

    # ── 判断 ───────────────────────────────────────────────────────
    def should_mitm(self, host: str) -> bool:
        host = (host or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.mitm_hosts)

    def should_inject(self, host: str, target: str,
                      headers: dict[str, str]) -> bool:
        """要不要往这个响应里注入代码。

        **只注解密核心那两个文件**（`/decrypt-video-core/…`）：
        * 密钥/密钥流只在 `worker_release.js` 里（`new Module.WxIsaac64(key)`、
          `decryptor.generate(131072)`、`p.decryptor_array`）；
        * 之前把 payload 撒到 `res.wx.qq.com` 上 ~60 个**全站共享**的 bundle 里，
          爆炸半径过大（把用户页面弄坏过两次）。现在缩到最小。
        **不碰 HTML、不动 CSP。**
        """
        if "res.wx.qq.com" not in (host or "").lower():
            return False
        if "/decrypt-video-core/" not in (target or ""):
            return False
        ctype = str(headers.get("content-type") or "").lower()
        if "javascript" in ctype or "ecmascript" in ctype:
            return True
        return target.split("?")[0].endswith(".js") and "html" not in ctype

    def handle_report(self, raw: bytes) -> int:
        """处理注入回报 —— **两条路径共用**这一个入口。

        坑（连白抓好几轮）：注入脚本把回报 POST 到**同源**的
        `https://channels.weixin.qq.com/__xydl_report`，所以它走的是
        CONNECT → MITM → `_relay`，而 `_relay` 里是**另一个**分支直接调
        `capture_injected`，压根不经过 `_local_capture` —— 我加的原始日志
        就一直没输出，数据被"未知标签"分支吞掉。现在统一到这里处理，
        并且**无条件打印原始内容**（不管 tag 是什么、能不能解析）。
        """
        text = raw.decode("utf-8", "replace")
        self.log("◀ 页面注入原始 " + text[:800])
        try:
            payload: Any = json.loads(text or "{}")
        except ValueError:
            payload = {}
        tag = str(payload.get("tag") or "") if isinstance(payload, dict) else ""
        # 密钥流：base64 解出来落盘，日志只记长度 + 指纹（不进日志，避免刷屏）
        if tag == "module" and isinstance(payload, dict) and payload.get("ksB64"):
            try:
                import base64
                import hashlib

                data = base64.b64decode(str(payload["ksB64"]) + "===")
                path = self.store.path.parent / "keystream.bin"
                path.write_bytes(data)
                self.log(f"★ 拿到密钥流 {len(data):,} 字节 → {path}"
                         f"  sha256={hashlib.sha256(data).hexdigest()[:16]}"
                         f"  来源={payload.get('here')}")
                return 0
            except Exception as exc:            # noqa: BLE001 - 解码失败不能影响页面
                self.log(f"⚠ 密钥流落盘失败：{type(exc).__name__}: {exc}")
                return 0
        if tag in ("alive", "heartbeat", "page-error", "worker", "worker-msg",
                   "module", "module-done", "worker-raw", "worker-hooked",
                   "probe"):
            # 诊断类：只记日志，不当数据落库
            # probe 给到 1500 字符：里面是"这个作用域里有哪些对象、键是什么"，
            # 是判断"代码有没有跑 / 该探哪个名字"的唯一依据。
            limit = 1500 if tag == "probe" else 600
            self.log(f"◀ 页面注入【{tag}】"
                     + json.dumps(payload, ensure_ascii=False)[:limit])
            return 0
        return self.capture_injected(payload)

    def capture_injected(self, payload: Any) -> int:
        """页面注入脚本 POST 回来的数据 → 落库。"""
        added = self.capture_payload(payload, source="inject")
        self.log(f"✓ 页面注入回报：新增 {added} 条"
                 f"（含 decode_key 的 {len(self.store.with_key())} 条）")
        return added

    def ssl_context_for(self, host: str) -> ssl.SSLContext | None:
        with self._cache_lock:
            ctx = self._ssl_cache.get(host)
            if ctx is not None:
                return ctx
        if self.ca is None:
            return None
        cert, key = self.ca.leaf_for(host)
        if not cert:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(str(cert), str(key))
        except (ssl.SSLError, OSError, ValueError) as exc:
            # 证书/私钥不配对时**绝不能把异常抛到请求线程**：那会把整个连接打崩、
            # 客户端（微信）直接报错，而且日志会被 traceback 刷满（实测一次刷了 198KB）。
            # 记一条日志、返回 None → 调用方退化成"直通隧道"，至少不影响用户上网。
            self.log(f"⚠ {host} 的证书不可用（{exc}），该域名退化为直通")
            return None
        with self._cache_lock:
            self._ssl_cache[host] = ctx
        return ctx

    # ── 上游连接 ───────────────────────────────────────────────────
    def upstream_endpoint(self) -> tuple[str, int]:
        """解析上游代理地址（``host:port``）。

        **必须容忍带 scheme 的写法。** 实测事故：命令行传进来的是
        ``http://127.0.0.1:7897``，早期实现直接 ``rpartition(":")`` 得到主机名
        ``http://127.0.0.1``，于是**每条非目标请求都 getaddrinfo 失败** ——
        用户机器上表现为"突然没网了"。这类事故代价太大，所以解析单独抽出来并配测试。
        """
        raw = (self.upstream or "").strip()
        if "://" in raw:
            raw = raw.split("://", 1)[1]
        raw = raw.split("/", 1)[0]              # 去掉可能带的路径
        if ":" in raw:
            host, _, port = raw.rpartition(":")
        else:
            host, port = raw, "80"              # 只给了主机名
        if not host:                            # 只给了 ":7897" 这种
            host = "127.0.0.1"
        return host, int(port or 80)

    def connect_upstream(self, host: str, port: int) -> socket.socket:
        mapped = self.upstream_map.get((host or "").lower())
        if mapped is not None:                     # 测试钩子：真实域名 → 本地服务
            return socket.create_connection(mapped, timeout=15)
        if not self.upstream:
            return socket.create_connection((host, port), timeout=15)
        up_host, up_port = self.upstream_endpoint()
        sock = socket.create_connection((up_host, up_port), timeout=15)
        request = (f"CONNECT {host}:{port} HTTP/1.1\r\n"
                   f"Host: {host}:{port}\r\n\r\n").encode()
        sock.sendall(request)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise ConnectionError("上游代理断开")
            head += chunk
        if b" 200" not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"上游代理拒绝：{head[:80]!r}")
        return sock

    def upstream_tls(self, sock: socket.socket, host: str) -> ssl.SSLSocket:
        if self.verify_upstream:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx.wrap_socket(sock, server_hostname=host)

    # ── 抓取 ───────────────────────────────────────────────────────
    def note_media_request(self, url: str) -> None:
        """看到 CDN 请求先把地址记下来（decode_key 可能要等 API 响应才到）。"""
        if looks_like_video_url(url):
            if self.store.add({"url": url, "decode_key": "", "title": "",
                               "object_id": "", "source": "cdn",
                               "ts": time.time()}):
                self.log(f"抓到媒体地址（待配对密钥）：{url[:80]}")

    def capture_payload(self, payload: Any, *, source: str = "api") -> int:
        """从任意 payload（页面注入发回来的也算）里存下 url/decode_key/档位。"""
        added = 0
        # 先收 spec[]：档位 URL 也带 decodeKey，会被通用 walker 一起捡走；
        # 先收才能带上 height/label/source=spec（按 url 去重，后到的只会补字段）。
        for spec in extract_specs(payload):
            if self.store.add({"url": spec["url"], "decode_key": spec["decode_key"],
                               "title": f"清晰度 {spec['label'] or spec['height']}",
                               "object_id": "", "source": "spec",
                               "height": spec["height"], "label": spec["label"],
                               "ts": time.time()}):
                self.stats["captured"] += 1
                self.log(f"✓ 抓到清晰度档位：{spec['label'] or '?'}"
                         f"（{spec['height'] or '?'}p）")
        for hit in extract_media(payload):
            entry = hit.as_entry(source)
            if self.store.add(entry):
                added += 1
                self.stats["captured"] += 1
                self.log(f"✓ 抓到视频地址 + decode_key"
                         f"（{'有密钥' if hit.decode_key else '无密钥'}）："
                         f"{hit.title[:24] or hit.url[:60]}")
        return added

    def capture_response(self, url: str, body: bytes, content_type: str, *,
                         source: str = "api") -> int:
        """从 HTTP 响应体里挖 url + decode_key。返回新增条数。"""
        self.stats["responses"] += 1
        ctype = (content_type or "").lower()
        if "json" not in ctype and not body.lstrip()[:1] == b"{":
            return 0
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return 0
        return self.capture_payload(payload, source=source)


class _ProxyHandler(BaseHTTPRequestHandler):
    """CONNECT 隧道 / 选择性中间人 / 普通 HTTP 转发。"""

    protocol_version = "HTTP/1.1"
    server_version = "xydl-capture"
    proxy: ChannelsProxy = None            # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:      # 静音默认访问日志
        self.proxy.log("  " + (fmt % args))

    # ── CONNECT ────────────────────────────────────────────────────
    def do_CONNECT(self) -> None:
        proxy = self.proxy
        proxy.stats["connect"] += 1
        host, _, port_text = self.path.partition(":")
        port = int(port_text or 443)
        if not proxy.should_mitm(host):
            proxy.stats["tunnel"] += 1
            self._tunnel(host, port)
            return
        proxy.stats["mitm"] += 1
        self._mitm(host, port)

    def _tunnel(self, host: str, port: int) -> None:
        """非目标域名：原样转发（优先交给用户原本的上游代理）。"""
        try:
            upstream = self.proxy.connect_upstream(host, port)
        except OSError as exc:
            self.send_error(502, f"connect failed: {exc}")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.close_connection = True
        _pump(self.connection, upstream)

    def _mitm(self, host: str, port: int) -> None:
        proxy = self.proxy
        ctx = proxy.ssl_context_for(host)
        if ctx is None:
            proxy.log(f"⚠ 没有 {host} 的证书，退化为直通（未解密）")
            self._tunnel(host, port)
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.close_connection = True
        try:
            client = ctx.wrap_socket(self.connection, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            proxy.log(f"⚠ 与客户端握手失败（{host}）：{exc}")
            return
        try:
            self._relay(client, host, port)
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            proxy.log(f"⚠ {host} 转发中断：{exc}")
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _relay(self, client: ssl.SSLSocket, host: str, port: int) -> None:
        """把客户端的一条请求转发到真实服务器，读回响应（顺手抓一把）。"""
        proxy = self.proxy
        stream = client.makefile("rb")
        try:
            start, headers = _read_head(stream)
        except ConnectionError:
            # 客户端只是**预连接**（Chromium 系很常见：先建好 TLS 再等真请求），
            # 这种情况没有请求可转发，安静收场，别在日志里报"转发中断"。
            return
        method, _, target = start.partition(" ")
        target = target.split(" ")[0]
        body = _read_body(stream, headers)
        full_url = f"https://{host}{target}" if target.startswith("/") else target

        # 注入脚本的回报请求：**同源**发到 channels.weixin.qq.com/__xydl_report，
        # 我们在这里就把它拦下并回 200 —— 不转发上游、不跨源、不碰 CSP。
        if target.split("?")[0] == REPORT_PATH:
            proxy.handle_report(body)
            payload = b'{"ok":true}'
            try:
                client.sendall(_build_response(
                    200, "OK", {"content-type": "application/json",
                                "content-length": str(len(payload))}, payload))
            except OSError:
                pass
            return

        if _INTERESTING_PATH.search(target):
            proxy.note_media_request(full_url)

        upstream = proxy.connect_upstream(host, port)
        try:
            tls = proxy.upstream_tls(upstream, host)
        except (ssl.SSLError, OSError):
            upstream.close()
            raise
        with tls:
            tls.sendall(_build_request(method, target,
                                       _forward_request_headers(headers, host), body))
            resp_stream = tls.makefile("rb")
            line, resp_headers = _read_head(resp_stream)
            parts = line.split(" ", 2)
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            reason = parts[2] if len(parts) > 2 else ""
            resp_body = _read_body(resp_stream, resp_headers)

        if status and _INTERESTING_PATH.search(target):
            proxy.capture_response(full_url, resp_body,
                                   resp_headers.get("content-type", ""))
        # 视频号页面 HTML：塞进注入脚本（拿 decodeKey/spec 的唯一路）
        inject = bool(getattr(proxy, "inject", False)) and proxy.should_inject(
            host, target, resp_headers)
        if inject:
            # 只注 JS；HTML 一律不碰、CSP 一律不动（上一版摘 CSP + 改 HTML
            # 把用户的视频号页面弄成打不开，教训写在 should_inject 的注释里）。
            resp_body = _inject_js(resp_body)
            proxy.stats["injected"] = proxy.stats.get("injected", 0) + 1
            proxy.log(f"✓ 已向页面 JS 注入抓取代码：{target[:160]}")
        if proxy.debug:
            # 诊断模式：把每个解密到的请求/响应都记下来，并把较大的响应体落盘。
            # 用处：微信客户端不一定用 JSON（可能是 protobuf 或别的路径），
            # 只按 JSON 匹配会"零捕获"，这时必须看真实报文才能改对。
            proxy.log(f"    {method} {target[:88]} → {status} "
                      f"{len(resp_body)}B {resp_headers.get('content-type','')[:44]}")
            if proxy.debug_dir and len(resp_body) >= 300:
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", target.split("?")[0])[:56]
                path = proxy.debug_dir / f"{int(time.time() * 1000)}-{safe[:40]}.bin"
                try:
                    path.write_bytes(resp_body)
                except OSError:
                    pass
        try:
            client.sendall(_build_response(status or 502, reason,
                                           resp_headers, resp_body))
        except OSError:
            pass

    # ── 本地接收端点：页面注入脚本往这儿 POST 抓到的数据 ─────────────
    def _local_capture(self) -> None:
        raw = b""
        query = urllib.parse.urlsplit(self.path).query
        params = urllib.parse.parse_qs(query)
        if "d" in params:                       # Image() 那条通道：?d=<json>
            raw = params["d"][0].encode("utf-8", "replace")
        else:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
        raw = b""
        added = 0
        try:
            added = self.proxy.handle_report(raw)
        except Exception as exc:               # 注入回报出错不能影响页面
            self.proxy.log(f"⚠ 注入回报处理失败：{type(exc).__name__}: {exc}")
        body = json.dumps({"ok": True, "added": added}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── 普通 HTTP（不走 CONNECT 的那部分，视频号基本用不到，保底）────
    def _plain(self, method: str) -> None:
        proxy = self.proxy
        url = self.path
        # 注入脚本请求的是 http://127.0.0.1:2023/__capture；如果系统代理把
        # 本地地址列入 bypass，它会以"源格式"（只有路径）直接打到我们端口上，
        # 所以两种形态都要认。
        if "/__capture" in url.split("?")[0]:
            self._local_capture()
            return
        if not url.lower().startswith("http"):
            self.send_error(400, "仅支持绝对 URL 的明文代理请求")
            return
        rest = url.split("://", 1)[1]
        host_port, _, target = rest.partition("/")
        host, _, port_text = host_port.partition(":")
        port = int(port_text or 80)
        body = _read_body(self.rfile, self.headers)
        headers = {k.lower(): v for k, v in self.headers.items()}

        if proxy.should_mitm(host) and _INTERESTING_PATH.search("/" + target):
            proxy.note_media_request(url)

        upstream = proxy.connect_upstream(host, port)
        try:
            upstream.sendall(_build_request(method, "/" + target,
                                            _forward_request_headers(headers, host),
                                            body))
            stream = upstream.makefile("rb")
            line, resp_headers = _read_head(stream)
            parts = line.split(" ", 2)
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            resp_body = _read_body(stream, resp_headers)
        finally:
            upstream.close()
        if status and proxy.should_mitm(host):
            proxy.capture_response(url, resp_body, resp_headers.get("content-type", ""))
        self.wfile.write(_build_response(status or 502,
                                         parts[2] if len(parts) > 2 else "",
                                         resp_headers, resp_body))

    def do_GET(self) -> None:      # noqa: N802
        self._plain("GET")

    def do_POST(self) -> None:     # noqa: N802
        self._plain("POST")

    def do_HEAD(self) -> None:     # noqa: N802
        self._plain("HEAD")


def _pump(a: socket.socket, b: socket.socket) -> None:
    """双向透明搬运（隧道模式）。"""
    sockets = [a, b]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                break
            for src in readable:
                dst = b if src is a else a
                try:
                    data = src.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
