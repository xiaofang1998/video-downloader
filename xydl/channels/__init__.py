"""渠道层：消息从哪来、回话往哪去。

**这里就是整套系统唯一需要为你自己的接单方式改动的部分。**

现实约束（很重要）：闲鱼没有开放 IM 接口，任何「自动回复机器人」都是逆向
闲鱼 App 协议 / 无障碍注入实现的，违反用户协议且有封号风险。所以默认提供
两个合规适配器：

* :class:`~xydl.channels.console.ConsoleChannel` —— 本地网页控制台，人机协同：
  运营把买家消息粘进来，系统跑完全流程后给出一段可直接复制回闲鱼的文案。
* :class:`~xydl.channels.inbox.InboxChannel` —— 目录收件箱：任何外部程序
  （包括你自己写的、或官方渠道的）往目录里丢一个文件就是一条消息，
  系统把回话写成同级文件。适合和已有工具解耦对接。

要接官方渠道（千牛客服 / 企业微信 / 飞书），继承 :class:`Channel`，
实现 ``deliver()`` 并在 ``start()`` 里把外部消息喂给 ``pipeline.submit_text()``
即可，业务逻辑一行都不用改。
"""

from __future__ import annotations

from .base import Channel, ChannelManager
from .console import ConsoleChannel, PortInUseError
from .inbox import InboxChannel

#: 名字 → 渠道类。加新渠道只要在这里登记一行。
CHANNEL_CLASSES: dict[str, type[Channel]] = {
    ConsoleChannel.name: ConsoleChannel,
    InboxChannel.name: InboxChannel,
}

__all__ = [
    "Channel",
    "ChannelManager",
    "ConsoleChannel",
    "InboxChannel",
    "PortInUseError",
    "CHANNEL_CLASSES",
    "build_channels",
]


def build_channels(pipeline, config) -> list[Channel]:
    """按 ``channels.*.enabled`` 实例化所有启用的渠道。"""
    out: list[Channel] = []
    for name, cls in CHANNEL_CLASSES.items():
        if not config.get(f"channels.{name}.enabled", False):
            continue
        try:
            out.append(cls(pipeline, config))
        except Exception as exc:  # noqa: BLE001 - 单个渠道装配失败不影响其他渠道
            print(f"[channels] {name} 启动配置有误，已跳过：{exc}")
    return out
