"""不配置任何上传通道时的占位实现。

存在的意义：让「网盘还没申请下来」这种状态也有一个合法配置值 ——
桥接渠道拿到 ``ok=False`` 会退回「只发文字话术」，而不是崩溃。
"""

from __future__ import annotations

from pathlib import Path

from .base import UploadResult, Uploader


class NullUploader(Uploader):
    name = "none"
    label = "未配置（只发文案，不发链接）"

    def upload(self, path: str | Path, remote_name: str = "") -> UploadResult:
        return UploadResult(
            ok=False,
            error="没有配置上传通道：把 delivery.uploader 设成 baidu 并填好凭据",
        )
