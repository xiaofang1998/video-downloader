"""领域模型：消息、链接候选、下载结果、订单状态机。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any
from uuid import uuid4


class OrderStatus(str, Enum):
    """订单状态机。

    RECEIVED ──► RESOLVING ──► RESOLVED ──► DOWNLOADING ──► DONE
                     │              │             │
                     └──────────────┴─────────────┴──► FAILED
                                                    └──► NEED_MANUAL（需人工兜底）
    """

    RECEIVED = "received"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    DOWNLOADING = "downloading"
    PACKAGING = "packaging"
    DONE = "done"
    FAILED = "failed"
    NEED_MANUAL = "need_manual"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.DONE,
            OrderStatus.FAILED,
            OrderStatus.NEED_MANUAL,
            OrderStatus.CANCELLED,
        }

    @property
    def is_active(self) -> bool:
        return not self.is_terminal


#: 状态 → 中文展示名（控制台用）
STATUS_LABEL: dict[str, str] = {
    OrderStatus.RECEIVED.value: "已接单",
    OrderStatus.RESOLVING.value: "解析链接",
    OrderStatus.RESOLVED.value: "已定位源",
    OrderStatus.DOWNLOADING.value: "下载中",
    OrderStatus.PACKAGING.value: "整理文件",
    OrderStatus.DONE.value: "已完成",
    OrderStatus.FAILED.value: "失败",
    OrderStatus.NEED_MANUAL.value: "需人工",
    OrderStatus.CANCELLED.value: "已取消",
}


@dataclass
class IncomingMessage:
    """渠道层投递给流水线的一条买家消息。"""

    channel: str
    conversation_id: str
    text: str
    sender: str = ""
    msg_id: str = ""
    ts: float = field(default_factory=time.time)
    #: 渠道自定义的附加信息（原样落库，核心流水线不解释它）。
    #: 闲鱼桥接用它携带 cookie_id / upstream_order_id，回传时才知道发给哪个账号。
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.msg_id:
            # 没有外部消息 id 时自动生成。必须带随机后缀：只用毫秒时间戳的话，
            # 同一会话在同一毫秒内提交两次会被误判成「重复消息」而被静默丢弃。
            self.msg_id = (
                f"{self.channel}:{self.conversation_id}:"
                f"{int(self.ts * 1000)}:{uuid4().hex[:8]}"
            )
        if not self.text.strip():
            raise ValueError("消息文本不能为空")

    @property
    def external_id(self) -> str:
        return f"{self.channel}:{self.msg_id}"


@dataclass
class LinkCandidate:
    """从消息里抽出来的一条链接候选。"""

    url: str
    platform: str = "unknown"
    platform_label: str = ""
    kind: str = "page"          # page | short | direct | drm | search
    source: str = "message"     # message | expanded | search | manual
    title: str = ""
    confidence: float = 0.5
    note: str = ""
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DownloadResult:
    """下载引擎的返回值。"""

    ok: bool
    file_path: str = ""
    title: str = ""
    size_bytes: int = 0
    duration_sec: float = 0.0
    engine: str = ""            # ytdlp | http
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def size_human(self) -> str:
        return human_size(self.size_bytes)


def human_size(num: float) -> str:
    """把字节数变成人能读的字符串。"""
    if not num or num <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    value = float(num)
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


@dataclass
class Order:
    """一条接单记录（与 SQLite 的 orders 表一一对应）。"""

    id: int | None = None
    external_id: str = ""
    channel: str = ""
    conversation_id: str = ""
    sender: str = ""
    text: str = ""
    status: str = OrderStatus.RECEIVED.value
    stage: str = ""
    progress: float = 0.0
    links: list[dict[str, Any]] = field(default_factory=list)
    chosen_url: str = ""
    title: str = ""
    file_path: str = ""
    file_size: int = 0
    reply: str = ""
    error: str = ""
    #: 渠道附加信息（见 IncomingMessage.meta）
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)

    @property
    def file_size_human(self) -> str:
        return human_size(self.file_size)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status_label"] = self.status_label
        data["file_size_human"] = self.file_size_human
        return data
