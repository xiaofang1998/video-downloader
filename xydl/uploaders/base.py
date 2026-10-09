"""上传通道抽象：把下载好的文件变成一条「能发出去的链接」。

**为什么必须有这一层**：闲鱼聊天只支持文本和图片，发不了视频文件。
所以「把视频交给买家」这件事，在现实中只能是：

    传到某个地方  →  拿到一条链接  →  用文本消息把链接发出去

这一层负责前半截。上层（桥接渠道）只认 :meth:`Uploader.upload` 的返回值，
所以换网盘 / 换对象存储 / 换成自建下载页，业务代码一行都不用改。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config


@dataclass
class UploadResult:
    """上传结果。``ok=False`` 时 ``error`` 一定非空。"""

    ok: bool
    #: 可以直接发给买家的链接
    url: str = ""
    #: 网盘提取码（没有就是空串）
    pwd: str = ""
    #: 云端路径 —— 排查用，不发给买家
    remote_path: str = ""
    #: 云端文件 id
    fs_id: str = ""
    error: str = ""
    #: 上层的原始响应，便于排查（不进正文）
    raw: dict[str, Any] = field(default_factory=dict)

    def reply_line(self, template: str = "") -> str:
        """把结果压成给买家看的一行/一段话。

        ``template`` 支持 ``{url}`` 和 ``{pwd}``；缺省给一个通顺的中文句式。
        提取码为空时自动省略那半句，不会出现「提取码：」后面空空如也。
        """
        if not self.ok or not self.url:
            return ""
        if template:
            merged = {"url": self.url, "pwd": self.pwd}
            try:
                return template.format_map(_SafePwd(merged)).strip()
            except (ValueError, IndexError, KeyError):
                return template.strip()
        line = f"网盘链接：{self.url}"
        if self.pwd:
            line += f"\n提取码：{self.pwd}"
        return line


class _SafePwd(dict):
    """模板里写了 ``{pwd}`` 但这次没有提取码时，别把字面量留在正文里。"""

    def __missing__(self, key: str) -> str:  # pragma: no cover - 兜底
        return ""


class Uploader(abc.ABC):
    """一个上传通道。

    子类实现 :meth:`upload`。**不要抛异常** —— 抛了会让订单交付整条断掉；
    失败就返回 ``UploadResult(ok=False, error=...)``，上层会退回「只发文案」。
    """

    name: str = "base"
    label: str = ""

    def __init__(self, config: Config):
        self.config = config

    @abc.abstractmethod
    def upload(self, path: str | Path, remote_name: str = "") -> UploadResult:
        """把 ``path`` 传上去，返回可分享的链接。"""

    def describe(self) -> str:
        return self.label or self.name
