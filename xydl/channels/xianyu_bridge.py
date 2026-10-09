"""闲鱼桥接渠道 —— 和上游「闲鱼超级管家」（xianyu-super-butler）对接。

数据流（时序 A：付款后才去翻聊天记录）：

    上游检测到「我已付款，等待你发货」
      → 从 ai_conversations 翻出买家最近发的链接
      → POST 本机 /api/orders  {text, conversation_id, channel: "xianyu", meta:{...}}
      → 本地流水线：识别 → 展开 → 下载
      → 本渠道 deliver()：上传网盘 → 拼好文案 → POST 上游 /api/xianyu/deliver
      → 上游把文案发回买家，并标记已发货

**为什么交付要绕网盘**：闲鱼聊天只支持文本和图片，发不了视频文件。
把视频传到网盘、发一条带提取码的文案，是唯一稳定可行的交付方式。

**和上游的边界**：这一层只做两件事 —— 收单（上游 POST 本地已有的
``/api/orders``）和回传（本地 POST 上游的 ``/api/xianyu/deliver``）。
上游那边需要的改动见 ``upstream_patch/``。
"""

from __future__ import annotations

import os
import threading
import time

from .base import Channel
from .. import http as http_util
from ..config import Config
from ..models import Order, OrderStatus
from ..uploaders import UploadResult, build_uploader
from ..utils import snippet


class XianyuBridgeChannel(Channel):
    name = "xianyu_bridge"
    label = "闲鱼桥接"

    def __init__(self, pipeline, config: Config):
        super().__init__(pipeline, config)
        self.upstream = str(
            config.get("delivery.bridge.upstream_url", "http://127.0.0.1:8080") or ""
        ).rstrip("/")
        self.deliver_path = str(
            config.get("delivery.bridge.deliver_path", "/api/xianyu/deliver")
        )
        self.token = str(config.get("delivery.bridge.token", "") or "")
        self.timeout = float(config.get("delivery.bridge.timeout_sec", 15) or 15)
        self.retries = max(0, int(config.get("delivery.bridge.retries", 2) or 0))
        #: 只处理这些来源渠道的订单。上游提交时把 channel 设成 xianyu；
        #: 人肉粘贴的 console 单默认**不会**被推回闲鱼，免得测试单发到真买家那儿。
        self.source_channels = {
            str(x).strip() for x in
            (config.get("delivery.bridge.source_channels", ["xianyu"]) or [])
            if str(x).strip()
        } or {"xianyu"}

        self.uploader = build_uploader(config)
        self.mode = str(config.get("delivery.mode", "share_link") or "share_link").lower()
        self.link_template = str(config.get("delivery.link_template", "") or "")
        # 进度话术默认不转发：买家不想被「进度 25%」刷屏。
        # 但「链接收到啦」这种接单确认是要发的，否则买家以为没人理他。
        self.forward_progress = bool(config.get("delivery.forward_progress", False))
        self.upload_fail_note = str(
            config.get("delivery.upload_fail_note", "") or ""
        ).strip()
        # upload_only 模式专用的提示：文件其实传上去了，只是没链接。
        self.upload_only_note = str(
            config.get("delivery.upload_only_note", "") or ""
        ).strip()

        #: 订单 id → 上传结果，保证一笔订单只上传一次
        self._uploads: dict[int, UploadResult] = {}
        self._lock = threading.Lock()

    # ── 生命周期 ───────────────────────────────────────────────────
    def start(self) -> None:
        super().start()

    # ── 投递 ───────────────────────────────────────────────────────
    def deliver(self, order: Order, text: str, file_path: str) -> None:
        """流水线有回话时被调用。**不抛异常** —— 抛了会被流水线记日志并忽略。"""
        if order.channel not in self.source_channels:
            return
        if not order.conversation_id:
            return
        if not text and not file_path:
            return
        # 下载进度话术默认不转发（见 forward_progress 的说明）。
        if (not file_path and not self.forward_progress
                and order.status == OrderStatus.DOWNLOADING.value):
            return

        body_text = text
        media: dict = {}
        if file_path and order.status == OrderStatus.DONE.value:
            result = self._upload_once(order, file_path)
            if self.mode == "upload_only":
                # 只上传、不发链接（分享权限还没批下来时的过渡模式）。
                # **注意这不是失败** —— 文件已经在账号里了，只是没生成分享链接，
                # 所以不能套用「上传失败」的安抚话术，那会让人以为白传了。
                if result.remote_path or result.fs_id:
                    self._log(order, f"已上传到网盘（未生成链接）：{result.remote_path}")
                    if self.upload_only_note:
                        body_text = f"{text}\n{self.upload_only_note}".strip()
                else:
                    self._log(order, f"上传失败：{result.error}", level="warn")
                    if self.upload_fail_note:
                        body_text = f"{text}\n{self.upload_fail_note}".strip()
            else:
                line = self._link_line(result)
                if line:
                    body_text = f"{text}\n{line}".strip() if text else line
                    media = {"url": result.url, "pwd": result.pwd,
                             "remote_path": result.remote_path, "fs_id": result.fs_id}
                    self._log(order, f"已上传网盘：{result.remote_path or result.url}")
                else:
                    reason = result.error or "上传通道未配置"
                    self._log(order, f"网盘交付不可用：{reason}", level="warn")
                    if self.upload_fail_note:
                        body_text = f"{text}\n{self.upload_fail_note}".strip()

        if not body_text:
            return
        self._post_delivery(order, body_text, file_path, media)

    # ── 上传 ───────────────────────────────────────────────────────
    def _upload_once(self, order: Order, file_path: str) -> UploadResult:
        with self._lock:
            cached = self._uploads.get(order.id)
        if cached is not None:
            return cached
        result = self.uploader.upload(file_path, remote_name=self._remote_name(order))
        with self._lock:
            self._uploads.setdefault(order.id, result)
            return self._uploads[order.id]

    @staticmethod
    def _remote_name(order: Order) -> str:
        """远端文件名带上本地订单号，避免并发下同名互相覆盖。"""
        base = os.path.basename(order.file_path or order.title or "video")
        return f"{order.id}_{base}"

    def _link_line(self, result: UploadResult) -> str:
        if self.mode == "upload_only":
            return ""
        return result.reply_line(self.link_template)

    # ── 回传上游 ───────────────────────────────────────────────────
    def _post_delivery(self, order: Order, text: str, file_path: str,
                       media: dict) -> bool:
        meta = order.meta if isinstance(order.meta, dict) else {}
        payload = {
            "local_order_id": order.id,
            "text": text,
            "conversation_id": order.conversation_id,
            # 上游要这两个字段才能找到「用哪个账号、发给谁」
            "cookie_id": meta.get("cookie_id", ""),
            "chat_id": meta.get("chat_id", "") or order.conversation_id,
            "buyer_id": meta.get("buyer_id", "") or order.sender,
            "upstream_order_id": meta.get("upstream_order_id", ""),
            "item_id": meta.get("item_id", ""),
            "status": order.status,
            "file_path": file_path or "",
            "media": media,
        }
        url = f"{self.upstream}{self.deliver_path}"
        headers = {"X-Auth-Token": self.token} if self.token else {}
        last_error = ""
        for attempt in range(self.retries + 1):
            # 上游就在本机，强制直连 —— 走代理只会把 localhost 请求送出去绕一圈。
            resp = http_util.request(
                "POST", url, json_body=payload, headers=headers,
                timeout=self.timeout, proxy="", verify_tls=False,
            )
            if resp.error:
                last_error = resp.error
            elif resp.status >= 400:
                last_error = f"HTTP {resp.status} {resp.text[:160]}"
            else:
                self._log(order, f"已回传上游（{snippet(text, 40)}）")
                return True
            if attempt < self.retries:
                time.sleep(0.5 * (attempt + 1))
        self._log(order, f"回传上游失败：{last_error}", level="error")
        return False

    # ── 辅助 ───────────────────────────────────────────────────────
    def _log(self, order: Order, message: str, level: str = "info") -> None:
        try:
            self.pipeline.store.log(order.id, f"[桥接] {message}", level=level)
        except Exception:  # noqa: BLE001 - 日志失败绝不能影响交付
            pass

    def ping(self) -> tuple[bool, str]:
        """探一下上游在不在。给 `run.py bridge-ping` 用。"""
        if not self.upstream:
            return False, "没配 delivery.bridge.upstream_url"
        resp = http_util.request(
            "GET", f"{self.upstream}/health", timeout=self.timeout,
            proxy="", verify_tls=False,
        )
        if resp.error:
            return False, f"{self.upstream}/health 连不上：{resp.error}"
        return resp.ok, f"{self.upstream}/health → HTTP {resp.status}"

    def describe(self) -> str:
        return f"{self.label} → {self.upstream}{self.deliver_path}（{self.uploader.describe()}）"
