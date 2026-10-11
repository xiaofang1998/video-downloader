"""用腾讯元宝把视频号分享链接解析成"带 token 的可播放链接"。

为什么需要它 —— 这是视频号下载链路的**入口**：

    用户粘 /sph/xxx 短链
      → 元宝 /api/weixin/get_parse_result  {"type":"video_channel_url","url":…,"scene":1}
      → data.playable_url = channels.weixin.qq.com/finder-preview/pages/feed?token=…&eid=…
      → 那个接口配上 token 下发 feedInfo.h264VideoInfo.videoUrl（明文 MP4）
      → 下载交付

几个实测要点（都别再踩）：

* 请求体必须是 ``{"type":"video_channel_url","url":…,"scene":1}``。只传 ``{"url":…}``
  服务端会返回**全空的骨架**（字段都在、值都是空串），看起来像"接口通了但没数据"。
* **鉴权不在 cookie 里**：实测浏览器里只有 6 个分析类 cookie（`_ga`/`_qimei_*`），
  真正的凭证在页面上下文（localStorage/请求头）里，Python 直接 POST 一定 401。
  所以必须**在页面里 fetch** —— 这条已经验证过两次（HTTP 200 + 完整数据）。
* 因此需要一个**登录过元宝的浏览器 profile**。本项目把它放在
  ``browser-login/yuanbao``，用 ``run.py channels-login --yuanbao`` 登录一次即可复用。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

from .cdp_fetch import _CDP, _find_edge, _http_json, _pick_port, login_profile_dir

LogFn = Callable[[str], None]
PARSE_API = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
HOME = "https://yuanbao.tencent.com/"
#: 判定登录态用的探测链接（公开的视频号分享短链，一直有效）
PROBE_LINK = "https://weixin.qq.com/sph/AJizVCRhAt"


def _noop(_m: str) -> None:
    pass


def _js_fetch(share_url: str) -> str:
    """页面上下文里的 fetch 表达式。

    ⚠️ ``'\\n'`` 必须写成双反斜杠：Python 里写 ``'\n'`` 会在 JS 单引号字符串里
    变成真正的换行 → **整段 JS 语法错误** → ``Runtime.evaluate`` 抛异常、
    返回没有 value。表现是"元宝返回：(空)"，害我误判成"接口没数据"查了半天。
    """
    return """
    (async () => {
      try {
        const r = await fetch('/api/weixin/get_parse_result', {
          method: 'POST', credentials: 'include',
          headers: {'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'},
          body: JSON.stringify({type: 'video_channel_url', url: %s, scene: 1})});
        return 'HTTP ' + r.status + '\\n' + (await r.text());
      } catch (e) { return 'ERR ' + e; }
    })()
    """ % json.dumps(share_url)


def parse_playable_url(share_url: str, *, profile: Path | None = None,
                       headless: bool = False, timeout: float = 90.0,
                       log: LogFn = _noop) -> tuple[str, str]:
    """把分享链接解析成带 token 的 playable_url。返回 ``(url, 错误)``。

    两条路，先便宜的后贵的：

    1. 页内直接 fetch 解析接口（快，但要页面已经有会话上下文）；
    2. **把链接发进元宝对话框**，抓它自己发出的解析响应（实测这是它真正的工作方式）。

    **默认有窗口模式**：实测 `--headless=new` 下页内 fetch 返回空值。
    """
    profile = Path(profile) if profile else login_profile_dir("yuanbao")
    profile.mkdir(parents=True, exist_ok=True)
    port = _pick_port() + 97
    args = [_find_edge(), f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check"]
    if headless:
        # 无头模式的默认 UA 里带 "HeadlessChrome"，元宝会当成未登录（实测）。
        # 覆盖成真实 Edge UA 才能复用登录态。
        args += ["--headless=new",
                 "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
                 "Safari/537.36 Edg/131.0.0.0"]
    args.append(HOME)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        page = None
        deadline = time.time() + 40
        while time.time() < deadline:
            time.sleep(1)
            try:
                pages = _http_json(f"http://127.0.0.1:{port}/json")
            except Exception:
                continue
            page = next((p for p in pages if p.get("type") == "page"
                         and "yuanbao" in str(p.get("url", ""))), None)
            if page:
                break
        if not page:
            return "", "元宝页面没起来（Edge 启动失败？）"

        cdp = _CDP(page["webSocketDebuggerUrl"], timeout=10.0)
        cdp.call("Network.enable")
        cdp.call("Page.enable")

        def evaluate(expr: str):
            r = cdp.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                              "returnByValue": True})
            return ((r.get("result") or {}).get("result") or {}).get("value")

        # 给应用初始化留时间：页面刚 load 完时执行上下文可能正在被前端重定向销毁，
        # 这时 evaluate 会拿不到值（表现为"(空)"，容易误判成接口没数据）。
        time.sleep(10)

        # 等页面把应用初始化完（无头模式下更慢），再**重试**页内 fetch：
        # 发早了会拿到空值（实测 "元宝返回：(空)"），看起来像接口没数据。
        raw = ""
        last_error = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = str(evaluate("(document.body?document.body.innerText:'').slice(0,200)")
                       or "")
            if "未登录" in text:
                return "", ("元宝没有登录态。先跑一次 "
                            "`run.py channels-login --yuanbao` 扫码登录（只需一次）")
            raw = str(evaluate(_js_fetch(share_url)) or "")
            log(f"元宝返回：{raw[:120] or '(空)'}")
            if '"playable_url"' in raw:
                break
            first_line = raw.split("\n")[0]
            if first_line.startswith("HTTP") and " 200" not in first_line:
                last_error = f"元宝接口返回 {first_line.strip()}"
            time.sleep(3)
        else:
            return "", last_error or "元宝解析超时（页面可能没加载完）"
        if raw.startswith("ERR"):
            return "", f"元宝页面请求失败：{raw[:120]}"
        head, _, body = raw.partition("\n")
        if " 200" not in head:
            return "", f"元宝接口返回 {head.strip() or '(空)'}"
        try:
            payload = json.loads(body)
        except ValueError:
            return "", f"元宝返回不是 JSON：{body[:120]}"
        if int(payload.get("code") or 0) != 0:
            return "", f"元宝解析失败：{payload.get('msg') or payload.get('code')}"
        playable = str((payload.get("data") or {}).get("playable_url") or "")
        if not playable:
            return "", ("元宝解析结果里没有 playable_url（这条链接可能不支持，"
                        "或需要换成视频号原分享链接）")
        return playable, ""
    finally:
        if cdp is not None:
            try:
                cdp.close()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass


def _js_fill_input(share_url: str) -> str:
    """把链接塞进元宝的输入框（React 受控组件，必须走原生 setter + 派发 input）。"""
    return """
    (() => {
      const sel = 'textarea, [contenteditable="true"], div[role="textbox"]';
      const box = document.querySelector(sel);
      if (!box) return 'NO_INPUT';
      box.focus();
      if (box.tagName === 'TEXTAREA' || box.tagName === 'INPUT') {
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(box, %s);
        box.dispatchEvent(new Event('input', {bubbles: true}));
        box.dispatchEvent(new Event('change', {bubbles: true}));
      } else {
        box.textContent = %s;
        box.dispatchEvent(new InputEvent('input', {bubbles: true}));
      }
      return 'FILLED';
    })()
    """ % (json.dumps(share_url), json.dumps(share_url))


def _js_click_send() -> str:
    """点发送按钮（找不到就返回 NO_BUTTON，由调用方改用回车）。"""
    return """
    (() => {
      const cands = [...document.querySelectorAll('button, [class*="send"], [class*="send-btn"]')];
      const btn = cands.find(b => /发送|send/i.test((b.innerText || '') + (b.className || '')));
      if (!btn) return 'NO_BUTTON';
      btn.click();
      return 'CLICKED';
    })()
    """


def parse_via_chat(share_url: str, *, profile: Path | None = None,
                   timeout: float = 120.0, log: LogFn = _noop) -> tuple[str, str]:
    """在元宝对话框里**把链接发出去**，抓它自己发的解析响应。

    为什么不用直接 fetch：元宝的解析是"收到消息"后触发的（实测它的前端请求
    ``{"type":"video_channel_url","url":…,"scene":1}`` 是在用户发送消息时才发出）。
    这里用 CDP 填输入框 + 发送，然后监听 ``/api/weixin/get_parse_result`` 的响应体。
    """
    profile = Path(profile) if profile else login_profile_dir("yuanbao")
    port = _pick_port() + 103
    proc = subprocess.Popen(
        [_find_edge(), f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", HOME],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        page = None
        deadline = time.time() + 45
        while time.time() < deadline:
            time.sleep(1)
            try:
                pages = _http_json(f"http://127.0.0.1:{port}/json")
            except Exception:
                continue
            page = next((p for p in pages if p.get("type") == "page"
                         and "yuanbao" in str(p.get("url", ""))), None)
            if page:
                break
        if not page:
            return "", "元宝页面没起来"
        cdp = _CDP(page["webSocketDebuggerUrl"], timeout=10.0)
        cdp.call("Network.enable")
        time.sleep(8)

        def ev(expr: str):
            r = cdp.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                              "returnByValue": True})
            return ((r.get("result") or {}).get("result") or {}).get("value")

        text = str(ev("(document.body?document.body.innerText:'').slice(0,200)") or "")
        if "未登录" in text:
            return "", ("元宝没有登录态。先跑一次 `run.py channels-login --yuanbao`"
                        "扫码登录（只需一次）")

        filled = str(ev(_js_fill_input(share_url)) or "")
        log(f"填输入框：{filled}")
        if filled != "FILLED":
            return "", f"没找到元宝输入框（{filled}）"
        time.sleep(0.6)
        clicked = str(ev(_js_click_send()) or "")
        log(f"发送按钮：{clicked}")
        if clicked != "CLICKED":
            # 回车兜底
            cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter",
                                                "code": "Enter", "windowsVirtualKeyCode": 13})
            cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter",
                                                "code": "Enter", "windowsVirtualKeyCode": 13})

        # 等它自己发解析请求，读响应体
        deadline = time.time() + timeout
        while time.time() < deadline:
            event = cdp.wait_event(
                lambda d: d.get("method") == "Network.responseReceived" and
                "get_parse_result" in str(((d.get("params") or {}).get("response") or {})
                                          .get("url") or ""), timeout=5)
            if event is None:
                continue
            rid = (event.get("params") or {}).get("requestId")
            try:
                body = cdp.call("Network.getResponseBody", {"requestId": rid})
                raw = str(((body.get("result") or {}).get("body")) or "")
            except Exception:
                continue
            log(f"抓到解析响应：{raw[:160]}")
            try:
                payload = json.loads(raw)
                playable = str((payload.get("data") or {}).get("playable_url") or "")
            except ValueError:
                playable = ""
            if playable:
                return playable, ""
            return "", "元宝解析响应里没有 playable_url（这条链接可能不支持）"
        return "", "发出去后没等到元宝的解析响应（超时）"
    finally:
        if cdp is not None:
            try:
                cdp.close()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass


def check_login(*, profile: Path | None = None,
                log: LogFn = _noop) -> tuple[bool, str]:
    """只检查元宝登录态（不起窗口等用户操作）。"""
    playable, error = parse_playable_url(
        "https://weixin.qq.com/sph/AJizVCRhAt", profile=profile, log=log, timeout=25)
    if playable:
        return True, "元宝登录态可用（已能解析短链）"
    return False, error or "元宝登录态不可用"


def login(headless: bool = False, log: LogFn = _noop,
          wait_sec: float = 240.0) -> tuple[bool, str]:
    """打开元宝登录页，等用户扫码。登录态落在工具自管 profile 里，之后可无头复用。"""
    profile = login_profile_dir("yuanbao")
    profile.mkdir(parents=True, exist_ok=True)
    port = _pick_port() + 101
    proc = subprocess.Popen(
        [_find_edge(), f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", HOME],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        page = None
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(1)
            try:
                pages = _http_json(f"http://127.0.0.1:{port}/json")
            except Exception:
                continue
            page = next((p for p in pages if p.get("type") == "page"
                         and "yuanbao" in str(p.get("url", ""))), None)
            if page:
                break
        if not page:
            return False, "元宝页面没起来"
        cdp = _CDP(page["webSocketDebuggerUrl"], timeout=10.0)
        cdp.call("Runtime.enable")

        def ev(expr: str):
            r = cdp.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                              "returnByValue": True})
            return ((r.get("result") or {}).get("result") or {}).get("value")

        log("请在弹出的窗口里扫码登录元宝…")
        deadline = time.time() + wait_sec
        nxt = 0.0
        while time.time() < deadline:
            time.sleep(4)
            # **必须用一次真实解析来判定登录**，不能看页面文字：
            # 页面刚加载、还没渲染出"未登录"字样时会假阳性（实测踩过）。
            probe = ""
            try:
                probe = str(ev(_js_fetch(PROBE_LINK)) or "")
            except Exception:
                probe = ""
            if '"playable_url":"http' in probe.replace(" ", ""):
                return True, "元宝已登录（登录态已存在工具自管 profile 里）"
            if time.time() > nxt:
                nxt = time.time() + 30
                log("… 等待扫码" + ("（页面还没登录）" if "未登录" in probe else ""))
        return False, "超时未检测到登录（请确认扫码后页面已进入元宝主界面）"
    finally:
        if cdp is not None:
            try:
                cdp.close()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
