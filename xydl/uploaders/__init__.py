"""上传通道层：把文件变成一条能发给买家的链接。

``delivery.uploader`` 选实现。加新通道只要在这里登记一行。
"""

from __future__ import annotations

from ..config import Config
from .base import Uploader, UploadResult
from .baidu import BaiduPanUploader
from .none import NullUploader
from .s3 import S3Uploader

#: 名字 → 类
UPLOADER_CLASSES: dict[str, type[Uploader]] = {
    BaiduPanUploader.name: BaiduPanUploader,
    S3Uploader.name: S3Uploader,
    NullUploader.name: NullUploader,
}

__all__ = [
    "Uploader",
    "UploadResult",
    "BaiduPanUploader",
    "S3Uploader",
    "NullUploader",
    "UPLOADER_CLASSES",
    "build_uploader",
]


def build_uploader(config: Config) -> Uploader:
    """按 ``delivery.uploader`` 造一个上传器；不认识的名字退回 NullUploader。

    刻意**不抛异常**：交付通道配错不该让整个服务起不来，
    退回 Null 的后果只是「只发文案、不发链接」，是安全的降级。
    """
    name = str(config.get("delivery.uploader", "none") or "none").strip().lower()
    cls = UPLOADER_CLASSES.get(name)
    if cls is None:
        print(f"[uploader] 不认识的上传通道 {name!r}，已退回 none")
        cls = NullUploader
    return cls(config)
