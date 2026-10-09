"""目录收件箱 —— 和「任何东西」解耦对接的渠道。

约定极简：往 ``inbox_dir`` 里丢一个文件，就是一条买家消息。
系统跑完后会把回话写成同级目录的 ``<原名>.reply.txt`` 和产物路径。

``.txt`` 的格式（元信息可选）::

    conversation: buyer123
    sender: 张三
    ---
    7.92 复制打开抖音，看看【@小明 的作品】... https://v.douyin.com/xxxx/

``.json`` 更直接::

    {"conversation_id": "buyer123", "sender": "张三", "text": "https://..."}

为什么要有这个渠道：它把「消息怎么来」彻底和业务解耦。你以后写任何对接
（哪怕是官方渠道的 webhook），只要能往目录里写文件，就能驱动整套流水线。
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

from .base import Channel
from ..config import Config
from ..models import IncomingMessage, Order
from ..utils import now_str

#: 元信息行的分隔符
SEPARATOR = "---"
META_KEYS = {"conversation", "conversation_id", "sender", "msg_id", "id"}


class InboxChannel(Channel):
    """轮询目录当收件箱。"""

    name = "inbox"
    label = "目录收件箱"

    def __init__(self, pipeline, config: Config):
        super().__init__(pipeline, config)
        self.root = config.path_of("inbox_dir")
        self.processed_dir = self.root / "processed"
        self.failed_dir = self.root / "failed"
        self.poll_sec = max(0.2, float(config.get("channels.inbox.poll_sec", 2.0) or 2.0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        #: 订单 id → 回话要写进哪个文件
        self._reply_paths: dict[int, Path] = {}

    def start(self) -> None:
        super().start()
        for directory in (self.root, self.processed_dir, self.failed_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name="xydl-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # ── 轮询 ───────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:  # noqa: BLE001 - 扫描线程不能死
                print(f"[inbox] 扫描出错：{type(exc).__name__}: {exc}")
            self._stop.wait(self.poll_sec)

    def _scan_once(self) -> int:
        count = 0
        for path in sorted(self.root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in (".txt", ".json"):
                continue
            # 半写入的文件先跳过（调用方可能还在写）
            if time.time() - path.stat().st_mtime < 0.2:
                continue
            if self._handle_file(path):
                count += 1
        return count

    def _handle_file(self, path: Path) -> bool:
        try:
            message = self.parse_file(path)
        except Exception as exc:  # noqa: BLE001
            target = self.failed_dir / path.name
            try:
                shutil.move(str(path), str(target))
                target.with_suffix(target.suffix + ".error.txt").write_text(
                    f"{now_str()} 解析失败：{type(exc).__name__}: {exc}\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
            return False

        if message is None:
            return False

        order, created = self.pipeline.submit(message)
        target = self.processed_dir / path.name
        try:
            shutil.move(str(path), str(target))
        except OSError:
            pass

        reply_path = self.processed_dir / f"{path.stem}.reply.txt"
        with self._lock:
            self._reply_paths[order.id] = reply_path
        header = (f"# 订单 {order.id} · {now_str()}\n"
                  f"# 会话 {order.conversation_id} · 发送者 {order.sender or '-'}\n"
                  f"# 重复消息：{'否' if created else '是（已忽略）'}\n")
        reply_path.write_text(header, encoding="utf-8")
        return True

    @staticmethod
    def parse_file(path: Path) -> IncomingMessage | None:
        """把收件箱文件解析成一条消息。解析不出来抛异常。"""
        raw = path.read_text(encoding="utf-8-sig", errors="replace")

        if path.suffix.lower() == ".json":
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("JSON 必须是对象")
            text = str(data.get("text") or "").strip()
            if not text:
                raise ValueError("缺少 text 字段")
            return IncomingMessage(
                channel=str(data.get("channel") or "inbox"),
                conversation_id=str(data.get("conversation_id") or "inbox"),
                text=text,
                sender=str(data.get("sender") or ""),
                msg_id=str(data.get("msg_id") or path.stem),
                meta=dict(data.get("meta") or {}) if isinstance(data.get("meta"), dict) else {},
            )

        meta: dict[str, str] = {}
        body_lines: list[str] = []
        in_body = False
        for line in raw.splitlines():
            if not in_body and line.strip() == SEPARATOR:
                in_body = True
                continue
            if not in_body:
                if ":" in line:
                    key, _, value = line.partition(":")
                    if key.strip().lower() in META_KEYS:
                        meta[key.strip().lower()] = value.strip()
                        continue
                if line.strip():
                    # 第一行不是元信息 → 整份文件都是消息正文
                    in_body = True
                    body_lines.append(line)
            else:
                body_lines.append(line)

        text = "\n".join(body_lines).strip()
        if not text:
            return None   # 空文件，先留着（可能还在写）
        return IncomingMessage(
            channel="inbox",
            conversation_id=meta.get("conversation") or meta.get("conversation_id") or "inbox",
            text=text,
            sender=meta.get("sender", ""),
            msg_id=meta.get("msg_id") or meta.get("id") or path.stem,
        )

    # ── 投递 ───────────────────────────────────────────────────────
    def deliver(self, order: Order, text: str, file_path: str) -> None:
        with self._lock:
            target = self._reply_paths.get(order.id)
        if target is None:
            out_dir = self.root / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f"{order.id}.txt"
            with self._lock:
                self._reply_paths[order.id] = target

        stamp = now_str("%H:%M:%S")
        lines = [f"\n[{stamp}] {order.status_label}", text]
        if file_path:
            lines.append(f"文件：{file_path}")
        try:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def describe(self) -> str:
        return f"{self.label} {self.root}"
