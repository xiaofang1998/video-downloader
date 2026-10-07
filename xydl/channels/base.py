"""渠道抽象与生命周期管理。"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from ..config import Config
from ..models import Order

if TYPE_CHECKING:  # pragma: no cover
    from ..pipeline import Pipeline


class Channel(abc.ABC):
    """一个消息通道。

    子类要做两件事：
      1. ``start()`` —— 把自己接到 ``pipeline`` 上并开始收消息。
         收消息时调用 ``self.pipeline.submit_text(...)`` 或 ``submit(...)``。
      2. ``deliver()`` —— 流水线有回话时被调用，把话送到运营/买家那里。
    """

    name: str = "base"
    label: str = ""

    def __init__(self, pipeline: "Pipeline", config: Config):
        self.pipeline = pipeline
        self.config = config

    def start(self) -> None:
        """注册投递回调。子类如果重写，记得调 ``super().start()``。"""
        self.pipeline.add_deliverer(self.deliver)

    def stop(self) -> None:
        """释放资源。默认什么都不用做。"""

    @abc.abstractmethod
    def deliver(self, order: Order, text: str, file_path: str) -> None:
        """把一条回话送出去。**不要抛异常**，抛了会被流水线记日志并忽略。"""

    def describe(self) -> str:
        return self.label or self.name


class ChannelManager:
    """统一启动/停止所有渠道。

    刻意**不吞掉**启动异常：把失败收集起来交给调用方决定要不要退出。
    否则端口被占用这类致命问题会只打印一行日志，然后进程挂着什么都不干。
    """

    def __init__(self, channels: list[Channel]):
        self.channels = list(channels)
        self.errors: list[tuple[str, Exception]] = []

    def start(self) -> None:
        self.errors = []
        for channel in self.channels:
            try:
                channel.start()
            except Exception as exc:  # noqa: BLE001 - 逐个渠道隔离失败
                self.errors.append((channel.name, exc))

    def stop(self) -> None:
        for channel in self.channels:
            try:
                channel.stop()
            except Exception:  # noqa: BLE001
                pass
        self.errors = []

    @property
    def failed_names(self) -> list[str]:
        return [name for name, _ in self.errors]

    def describe(self) -> str:
        if not self.channels:
            return "（没有启用任何渠道）"
        ok = [c.describe() for c in self.channels
              if c.name not in self.failed_names]
        return "、".join(ok) if ok else "（所有渠道都启动失败）"
