"""回话文案：把订单状态翻译成能直接粘回闲鱼的话。

模板全部放在 ``config.json`` 的 ``replies`` 段里，运营可以随时改话术，
不用动代码。占位符用 ``{花括号}``，写错的占位符会原样保留而不是抛异常
—— 宁可买家看到 ``{title}``，也不能让整条流水线因为一句文案崩掉。
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .models import Order, OrderStatus, human_size
from .utils import snippet

#: 模板里可以用的全部占位符（写文档 / 做校验用）
PLACEHOLDERS = (
    "title", "filename", "size", "progress", "platform", "error", "url",
    "eta", "engine", "sender", "order_id", "duration",
)


class _SafeDict(dict):
    """缺失的占位符原样保留，不抛 KeyError。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def short_reason(error: str, limit: int = 40) -> str:
    """把技术错误压成一句给买家看的话。

    订单里存的是完整错误（运营在控制台能看全），但发给客户的话术里塞一段
    200 字的技术说明只会让人一脸问号。
    """
    if not error:
        return ""
    text = error
    for sep in ("；", ";", "。", "\n", "，"):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    return snippet(text.strip(), limit)


class Replies:
    """按当前配置渲染各种回话。"""

    def __init__(self, config: Config):
        self.config = config
        self.step = max(5, int(config.get("replies.progress_step", 25) or 25))

    # ── 渲染 ───────────────────────────────────────────────────────
    def render(self, key: str, **fields: Any) -> str:
        template = str(self.config.get(f"replies.{key}", "") or "")
        if not template:
            return ""
        merged = _SafeDict({name: "" for name in PLACEHOLDERS})
        merged.update({k: ("" if v is None else v) for k, v in fields.items()})
        try:
            return template.format_map(merged).strip()
        except (ValueError, IndexError, KeyError):
            # 模板写坏了（比如孤立的 { ），退回原文，至少还能用
            return template.strip()

    # ── 各状态 ─────────────────────────────────────────────────────
    def received(self, order: Order) -> str:
        return self.render("received", title=order.title, platform=order.channel)

    def progress(self, order: Order, percent: float, eta: str = "") -> str:
        return self.render(
            "downloading",
            progress=f"{percent:.0f}",
            title=order.title,
            eta=eta,
            url=order.chosen_url,
        )

    def done(self, order: Order) -> str:
        return self.render(
            "done",
            title=order.title,
            filename=order.file_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
            size=human_size(order.file_size),
            url=order.chosen_url,
        )

    def need_manual(self, order: Order, reason: str = "") -> str:
        base = self.render("need_manual", title=order.title, url=order.chosen_url)
        if reason and self.config.get("replies.append_reason", False):
            return f"{base}\n（原因：{snippet(reason, 80)}）"
        return base

    def failed(self, order: Order) -> str:
        # 给买家的只有一句短原因；完整错误留在 order.error 里给运营看
        return self.render("failed", title=order.title,
                           error=short_reason(order.error), url=order.chosen_url)

    def drm(self, order: Order, platform: str) -> str:
        return self.render("drm", platform=platform, title=order.title,
                           url=order.chosen_url)

    # ── 进度节流 ───────────────────────────────────────────────────
    def should_report_progress(self, percent: float, last_reported: float) -> bool:
        """只在跨过整数个 step 时打扰买家，避免刷屏。"""
        return percent - last_reported >= self.step

    def reply_for(self, order: Order) -> str:
        """按订单当前状态挑一条合适的回话（控制台「一键复制」用）。"""
        status = order.status
        if status == OrderStatus.DONE.value:
            return self.done(order)
        if status == OrderStatus.FAILED.value:
            return self.failed(order)
        if status == OrderStatus.NEED_MANUAL.value:
            return self.need_manual(order, order.error)
        if status == OrderStatus.DOWNLOADING.value:
            return self.progress(order, order.progress)
        if status == OrderStatus.RECEIVED.value:
            return self.received(order)
        return self.render("received", title=order.title, platform=order.channel)
