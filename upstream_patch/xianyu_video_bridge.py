"""把「视频代下载」这类订单交给本机的 xianyu-video-bot 处理。

放在上游「闲鱼超级管家」（xianyu-super-butler）**根目录**下，和
``XianyuAutoAsync.py`` 同级。整个文件是自包含的：只用标准库，
不改上游任何一行原有逻辑，出问题随时删掉这个文件即可恢复原状。

做两件事：
  1. ``handoff_to_video_bot()`` —— 检测到「我已付款」时，从聊天记录里翻出
     买家之前发的链接，POST 给本机的 video-bot，并**跳过**卡券发货。
  2. ``install_deliver_endpoint(app)`` —— 注册 ``POST /api/xianyu/deliver``，
     让 video-bot 下载完把文案回传过来，由上游发回给买家。

配置见同目录的 ``xianyu_video_bridge.json``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

# FastAPI 相关的导入**必须放在模块顶层**。
#
# 踩过的坑：原来的写法是在 install_deliver_endpoint() 里局部 import Request，
# 然后写成 `async def handler(request: Request)`。但 FastAPI 是用
# typing.get_type_hints() 在**函数的 __globals__（也就是本模块的全局命名空间）**
# 里解析注解的 —— 局部导入进不了那里，于是 Request 变成解析不掉的 ForwardRef，
# 路由建不起来，请求会以一个莫名其妙的
# `422 {"loc":["query","request"]}` 失败（看起来像参数写错，其实是注解炸了）。
#
# 用 try 包住是为了让这个文件在没装 FastAPI 的环境里也能导入（不影响其它功能）。
try:
    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse
except ImportError:  # pragma: no cover - 只在上游里才需要 FastAPI
    HTTPException = None  # type: ignore[assignment]
    Request = None        # type: ignore[assignment]
    JSONResponse = None   # type: ignore[assignment]

logger = logging.getLogger("xianyu_video_bridge")

#: 配置文件名（与上游启动目录同级）
CONFIG_NAME = "xianyu_video_bridge.json"

DEFAULT_CONFIG: dict = {
    # 总开关。关掉之后这个模块什么也不做，上游按原逻辑发卡券。
    "enabled": True,
    # 本机 video-bot 的地址（它默认监听 127.0.0.1:8765）
    "local_url": "http://127.0.0.1:8765",
    # video-bot 的 server.token。那边配了 token 这里必须一致，否则会被 401。
    "local_token": "",
    # 只有这些商品走桥接。留空 = 所有商品都走。
    # 提醒：一旦某商品走桥接，它的卡券发货就被跳过了，所以强烈建议显式列出。
    "item_ids": [],
    # 从聊天记录里往回找几条买家消息。买家可能先发链接、聊几句、再付款。
    "link_lookback": 30,
    # 哪些 role 算「买家说的话」。上游默认存 'user'。
    "buyer_roles": ["user"],
    # 回传接口的共享密钥。非空时 video-bot 必须带对 X-Auth-Token。
    "auth_token": "",
    # 回传后是否顺手把订单标成已发货
    "mark_shipped": True,
}

_config_cache: dict | None = None
_config_mtime: float = 0.0

#: 明显不该被当成「买家要下载的视频」的链接（我们自己发出去的网盘链接等）
_SKIP_URL_PATTERNS = (
    "pan.baidu.com", "aliyundrive.com", "alipan.com", "quark.cn",
    "goofish.com", "2.taobao.com", "taobao.com",
)

_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff，。！？、）】」』\"'<>]+", re.I)


# ══════════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════════
def load_config(force: bool = False) -> dict:
    """读配置。文件改了会自动重载，不用重启上游。"""
    global _config_cache, _config_mtime

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if _config_cache is None:
            _config_cache = dict(DEFAULT_CONFIG)
            logger.warning("[视频桥接] 没找到 %s，按默认配置运行（local_url=%s）",
                           CONFIG_NAME, _config_cache["local_url"])
        return _config_cache

    if force or _config_cache is None or mtime != _config_mtime:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            merged = dict(DEFAULT_CONFIG)
            if isinstance(data, dict):
                merged.update(data)
            _config_cache = merged
            _config_mtime = mtime
            logger.info("[视频桥接] 配置已加载：enabled=%s local_url=%s items=%s",
                        merged["enabled"], merged["local_url"],
                        merged["item_ids"] or "全部")
        except Exception as exc:  # noqa: BLE001 - 配置坏了不能拖垮上游
            logger.error("[视频桥接] 配置读取失败，沿用上一次的配置：%s", exc)
            if _config_cache is None:
                _config_cache = dict(DEFAULT_CONFIG)
    return _config_cache


def _http_json(url: str, payload: dict | None = None,
               headers: dict | None = None, timeout: float = 15.0) -> tuple[bool, str]:
    """发一个 JSON 请求。返回 (是否成功, 说明)。同步实现，调用方负责丢线程。"""
    data = None
    send_headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        send_headers["Content-Type"] = "application/json; charset=utf-8"
    send_headers.update(headers or {})

    req = urllib.request.Request(url, data=data, headers=send_headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            return 200 <= resp.status < 300, f"HTTP {resp.status} {body[:200]}"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return False, f"HTTP {exc.code} {detail}"
    except Exception as exc:  # noqa: BLE001 - 网络问题绝不能抛进上游
        return False, f"{type(exc).__name__}: {exc}"


# ══════════════════════════════════════════════════════════════════════
#  从聊天记录里找买家发的链接
# ══════════════════════════════════════════════════════════════════════
def find_buyer_link(cookie_id: str, buyer_id: str, lookback: int = 30,
                    roles: list[str] | None = None) -> str:
    """翻出这位买家最近一条「看起来是要下载的视频」的链接。

    为什么必须翻记录而不是等买家在付款消息里再发一次：闲鱼的付款通知是
    系统消息，里面只有订单 id。买家的链接是在**付款之前**发的。
    """
    roles = roles or ["user"]
    try:
        from db_manager import db_manager
    except Exception as exc:  # noqa: BLE001
        logger.error("[视频桥接] 拿不到 db_manager：%s", exc)
        return ""

    placeholders = ",".join("?" for _ in roles)
    sql = (
        "SELECT content FROM ai_conversations "
        f"WHERE cookie_id = ? AND user_id = ? AND role IN ({placeholders}) "
        "ORDER BY id DESC LIMIT ?"
    )
    try:
        with db_manager.lock:
            cursor = db_manager.conn.cursor()
            cursor.execute(sql, (cookie_id, buyer_id, *roles, int(lookback)))
            rows = cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.error("[视频桥接] 查聊天记录失败：%s", exc)
        return ""

    for row in rows or []:
        content = row[0] if not isinstance(row, dict) else row.get("content", "")
        for url in _URL_RE.findall(str(content or "")):
            low = url.lower()
            if any(skip in low for skip in _SKIP_URL_PATTERNS):
                continue
            return url
    return ""


# ══════════════════════════════════════════════════════════════════════
#  收到付款消息 → 交给 video-bot
# ══════════════════════════════════════════════════════════════════════
async def handoff_to_video_bot(live, message: dict, item_id: str, chat_id: str,
                               send_user_id: str, send_user_name: str = "") -> bool:
    """把订单转给 video-bot。

    返回 ``True`` 表示「这笔订单已经交出去了」，调用方应当 ``return``，
    不要再走卡券发货；``False`` 表示按原逻辑继续。
    """
    config = load_config()
    if not config.get("enabled", True):
        return False

    item_ids = [str(x) for x in (config.get("item_ids") or [])]
    if item_ids and str(item_id) not in item_ids:
        logger.info("[视频桥接] 商品 %s 不在桥接清单里，走原卡券发货", item_id)
        return False

    cookie_id = getattr(live, "cookie_id", "") or ""
    order_id = ""
    try:
        order_id = live._extract_order_id(message) or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("[视频桥接] 提取订单号失败（不影响下载）：%s", exc)

    # 买家链接：优先用这条消息里的，没有就翻历史
    text = ""
    try:
        text = _extract_message_text(message)
    except Exception:  # noqa: BLE001
        text = ""
    link = ""
    for url in _URL_RE.findall(text or ""):
        if not any(skip in url.lower() for skip in _SKIP_URL_PATTERNS):
            link = url
            break
    if not link:
        link = await asyncio.to_thread(
            find_buyer_link, cookie_id, send_user_id,
            int(config.get("link_lookback", 30)), list(config.get("buyer_roles") or ["user"]),
        )

    if not link:
        logger.warning(
            "[视频桥接] 订单 %s 没找到买家发的链接，只能回退到卡券发货。"
            "（买家可能在付款前没发链接，或 ai_conversations 的 role 字段不是 'user'）",
            order_id or "?",
        )
        return False

    payload = {
        "text": text or link,
        "conversation_id": chat_id,
        "sender": send_user_id,
        "channel": "xianyu",
        "meta": {
            "cookie_id": cookie_id,
            "chat_id": chat_id,
            "buyer_id": send_user_id,
            "buyer_name": send_user_name,
            "upstream_order_id": order_id,
            "item_id": str(item_id or ""),
            "link": link,
        },
    }
    headers = {}
    if config.get("local_token"):
        headers["X-Auth-Token"] = config["local_token"]

    url = config["local_url"].rstrip("/") + "/api/orders"
    ok, detail = await asyncio.to_thread(_http_json, url, payload, headers, 20.0)
    if not ok:
        logger.error("[视频桥接] 提交给 video-bot 失败（%s），回退到卡券发货：%s", url, detail)
        return False

    logger.info("[视频桥接] ✅ 订单 %s 已交给 video-bot 下载：%s", order_id or "?", link)
    return True


def _extract_message_text(message: dict) -> str:
    """从闲鱼消息体里抠出可读文本。抠不到就返回空串，不影响主流程。"""
    if not isinstance(message, dict):
        return ""
    message_1 = message.get("1")
    if isinstance(message_1, str):
        return message_1
    if isinstance(message_1, dict):
        for key in ("10", "6", "3"):
            node = message_1.get(key)
            if isinstance(node, dict):
                for sub in ("content", "text", "summary"):
                    if isinstance(node.get(sub), str):
                        return node[sub]
        # 兜底：整个字典转字符串再交给正则捞 URL
        return json.dumps(message_1, ensure_ascii=False)
    return ""


# ══════════════════════════════════════════════════════════════════════
#  回传：video-bot → 上游 → 买家
# ══════════════════════════════════════════════════════════════════════
def install_deliver_endpoint(app, path: str = "/api/xianyu/deliver") -> None:
    """把 ``POST /api/xianyu/deliver`` 挂到上游的 FastAPI 上。"""
    if Request is None:
        raise RuntimeError(
            "没有 FastAPI —— install_deliver_endpoint() 只能在上游项目里调用"
        )

    @app.post(path)
    async def _xianyu_video_deliver(request: Request):  # noqa: ANN202
        config = load_config()
        if not config.get("enabled", True):
            raise HTTPException(status_code=503, detail="视频桥接已关闭")

        auth = config.get("auth_token") or ""
        if auth and request.headers.get("X-Auth-Token") != auth:
            raise HTTPException(status_code=401, detail="X-Auth-Token 不对")

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text 不能为空")

        cookie_id = str(body.get("cookie_id") or "")
        chat_id = str(body.get("chat_id") or body.get("conversation_id") or "")
        buyer_id = str(body.get("buyer_id") or "")
        order_id = str(body.get("upstream_order_id") or "")
        if not (cookie_id and chat_id and buyer_id):
            raise HTTPException(
                status_code=400,
                detail="缺少 cookie_id / chat_id / buyer_id，无法定位闲鱼会话",
            )

        from XianyuAutoAsync import XianyuLive

        live = XianyuLive.get_instance(cookie_id)
        if live is None:
            raise HTTPException(
                status_code=409,
                detail=f"账号 {cookie_id} 当前没有在线实例（WebSocket 未连接）",
            )
        ws = getattr(live, "ws", None)
        if ws is None or getattr(ws, "closed", False):
            raise HTTPException(status_code=409, detail="该账号 WebSocket 已断开，稍后重试")

        try:
            await live.send_msg(ws, chat_id, buyer_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.error("[视频桥接] 发送消息失败：%s", exc)
            raise HTTPException(status_code=502, detail=f"发送消息失败：{exc}")

        logger.info("[视频桥接] ✅ 已把回话发给买家（订单 %s）", order_id or "?")

        if order_id and config.get("mark_shipped", True):
            try:
                from db_manager import db_manager
                db_manager.insert_or_update_order(
                    order_id=order_id, order_status="shipped", system_shipped=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[视频桥接] 标记已发货失败（消息已发出）：%s", exc)

        return JSONResponse({"ok": True, "order_id": order_id, "at": time.time()})

    logger.info("[视频桥接] 已注册回传端点 %s", path)
