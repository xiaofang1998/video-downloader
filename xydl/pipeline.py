"""流水线编排：一条买家消息从进来到出文件的完整生命周期。

线程模型很简单，也够用：
  * 一个 :class:`queue.Queue` 装待处理的订单 id；
  * N 个 worker 线程（``download.workers``）各跑一个订单；
  * 每个订单一个独立子目录，所以 worker 之间不会互相踩文件。

所有状态变更都先落 SQLite 再往外发，进程被 kill 也不会丢单。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable

from . import linkparse
from .config import Config
from .downloader import DownloadResult, Downloader, Progress, job_dir_for
from .models import IncomingMessage, Order, OrderStatus
from .replies import Replies
from .resolver import Resolver
from .store import Store
from .utils import (
    ensure_unique,
    is_generic_title,
    pick_best_title,
    sanitize_filename,
    snippet,
)

#: 通知回调：渠道层注册自己，订单有话说时会被调用
Deliverer = Callable[[Order, str, str], None]

#: 进度落库的最小间隔，避免把 SQLite 写爆
STORE_PROGRESS_INTERVAL = 0.5


class Pipeline:
    """接单 → 解析 → 下载 → 回话。"""

    def __init__(self, config: Config, store: Store, *,
                 downloader: Downloader | None = None,
                 resolver: Resolver | None = None,
                 replies: Replies | None = None):
        self.config = config
        self.store = store
        self.downloader = downloader or Downloader(config)
        self.resolver = resolver or Resolver(config)
        self.replies = replies or Replies(config)

        self.download_dir = config.path_of("download_dir")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.workers = max(1, int(config.get("download.workers", 2) or 2))

        self._queue: queue.Queue[int] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._deliverers: list[Deliverer] = []
        self._cancel_flags: dict[int, threading.Event] = {}
        self._lock = threading.RLock()
        self._running = False

    # ── 生命周期 ───────────────────────────────────────────────────
    def add_deliverer(self, fn: Deliverer) -> None:
        """注册一个「话要往哪送」的回调（渠道层在 start() 里调用）。"""
        with self._lock:
            self._deliverers.append(fn)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for i in range(self.workers):
            thread = threading.Thread(target=self._worker_loop, name=f"xydl-worker-{i}",
                                      daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        self._running = False
        for order_id, flag in list(self._cancel_flags.items()):
            flag.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    # ── 投递 ───────────────────────────────────────────────────────
    def submit(self, message: IncomingMessage, *, auto_reply: bool = True) -> tuple[Order, bool]:
        """接一条消息。返回 (订单, 是否新单)。重复消息不会重复下载。"""
        order, created = self.store.create_order(
            external_id=message.external_id,
            channel=message.channel,
            conversation_id=message.conversation_id,
            sender=message.sender,
            text=message.text,
        )
        if not created:
            self.store.log(order.id, "重复消息，忽略")
            return order, False

        if auto_reply:
            self._emit(order, self.replies.received(order))

        self._queue.put(order.id)
        return order, True

    def submit_text(self, text: str, conversation_id: str = "manual",
                    channel: str = "console", sender: str = "",
                    auto_reply: bool = True) -> tuple[Order, bool]:
        """便捷入口：直接给一段文本。"""
        message = IncomingMessage(
            channel=channel, conversation_id=conversation_id, text=text, sender=sender,
        )
        return self.submit(message, auto_reply=auto_reply)

    def retry(self, order_id: int, auto_reply: bool = True) -> bool:
        """把失败/需人工的订单重新排队。"""
        order = self.store.get(order_id)
        if order is None or order.status in (OrderStatus.DOWNLOADING.value,
                                             OrderStatus.RESOLVING.value,
                                             OrderStatus.RESOLVED.value):
            return False
        self.store.update(order_id, status=OrderStatus.RECEIVED.value, stage="重新处理",
                          progress=0.0, error="")
        self.store.log(order_id, "人工触发重试")
        refreshed = self.store.get(order_id)
        if refreshed and auto_reply:
            self._emit(refreshed, self.replies.received(refreshed))
        self._queue.put(order_id)
        return True

    def cancel(self, order_id: int) -> bool:
        """取消订单：正在跑就置位取消标志，还没跑就直接改状态。"""
        with self._lock:
            flag = self._cancel_flags.get(order_id)
        if flag is not None:
            flag.set()
            self.store.log(order_id, "收到取消请求", level="warn")
            return True

        order = self.store.get(order_id)
        if order is None or OrderStatus(order.status).is_terminal:
            return False
        self.store.update(order_id, status=OrderStatus.CANCELLED.value,
                          stage="已取消", error="任务已取消")
        self.store.log(order_id, "任务被取消", level="warn")
        return True

    # ── 通知 ───────────────────────────────────────────────────────
    def _emit(self, order: Order, text: str, file_path: str = "") -> None:
        """把一条要回给买家的话写进 outbox 并广播给渠道层。"""
        if not text:
            return
        self.store.push_outbox(order.id, order.conversation_id, text, file_path)
        self.store.log(order.id, f"回复：{snippet(text, 100)}")
        with self._lock:
            deliverers = list(self._deliverers)
        for deliver in deliverers:
            try:
                deliver(order, text, file_path)
            except Exception as exc:  # noqa: BLE001 - 一个渠道坏了不能拖垮流水线
                self.store.log(order.id, f"投递失败：{type(exc).__name__}: {exc}",
                               level="error")

    # ── worker ────────────────────────────────────────────────────
    def _worker_loop(self) -> None:
        while self._running:
            try:
                order_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process(order_id)
            except Exception as exc:  # noqa: BLE001 - worker 绝不能死
                self.store.log(order_id, f"处理异常：{type(exc).__name__}: {exc}",
                               level="error")
                self.store.update(order_id, status=OrderStatus.FAILED.value,
                                  stage="内部错误", error=f"{type(exc).__name__}: {exc}")
            finally:
                self._queue.task_done()

    # ── 单笔订单 ───────────────────────────────────────────────────
    def _process(self, order_id: int) -> None:
        order = self.store.get(order_id)
        if order is None:
            return
        if order.status not in (OrderStatus.RECEIVED.value,):
            self.store.log(order_id, f"状态是 {order.status}，跳过")
            return

        cancel = threading.Event()
        with self._lock:
            self._cancel_flags[order_id] = cancel

        try:
            self._do_process(order, cancel)
        except Exception as exc:  # noqa: BLE001
            self.store.log(order_id, f"异常：{type(exc).__name__}: {exc}", level="error")
            self.store.update(order_id, status=OrderStatus.FAILED.value,
                              stage="异常中断", error=f"{type(exc).__name__}: {exc}")
            fresh = self.store.get(order_id)
            if fresh:
                self._emit(fresh, self.replies.failed(fresh))
        finally:
            with self._lock:
                self._cancel_flags.pop(order_id, None)

    def _do_process(self, order: Order, cancel: threading.Event) -> None:
        order_id = order.id  # type: ignore[assignment]

        # ── 1. 识别链接 ────────────────────────────────────────────
        self.store.update(order_id, status=OrderStatus.RESOLVING.value,
                          stage="识别链接", progress=1.0)
        parsed = linkparse.parse(order.text)
        self.store.update(order_id, links=[c.to_dict() for c in parsed.links])
        self.store.log(
            order_id,
            f"识别到 {len(parsed.links)} 条链接（{linkparse.describe(parsed.links)}）；"
            f"标题猜测={parsed.title_guess!r}",
        )

        # ── 2. 展开短链 / 搜索落源 ─────────────────────────────────
        self.store.update(order_id, stage="展开短链 / 搜索落源", progress=5.0)
        outcome = self.resolver.resolve(parsed.links, parsed.title_guess)
        for err in outcome.errors:
            self.store.log(order_id, err, level="warn")

        if cancel.is_set():
            self._finish_cancelled(order_id)
            return

        if outcome.drm_blocked:
            platform = parsed.links[0].platform_label if parsed.links else "该平台"
            self.store.update(order_id, status=OrderStatus.NEED_MANUAL.value,
                              stage="受版权保护", error=outcome.note,
                              title=parsed.title_guess or platform)
            fresh = self.store.get(order_id)
            if fresh:
                self._emit(fresh, self.replies.drm(fresh, platform))
            return

        if not outcome.ok or outcome.chosen is None:
            self.store.update(order_id, status=OrderStatus.NEED_MANUAL.value,
                              stage="需要人工", error=outcome.note,
                              title=outcome.title or parsed.title_guess)
            fresh = self.store.get(order_id)
            if fresh:
                self._emit(fresh, self.replies.need_manual(fresh, outcome.note))
            return

        chosen = outcome.chosen
        # 刻意不加 "video" 之类的兜底名：没有真标题时，让文件名保持原样
        # （直链 URL 里的文件名通常比一个占位名有信息量得多）
        title = (outcome.title or chosen.title or parsed.title_guess or "").strip()
        self.store.update(
            order_id,
            status=OrderStatus.RESOLVED.value,
            stage=f"已定位 {chosen.platform_label}",
            chosen_url=chosen.url,
            title=title,
            progress=10.0,
            links=[c.to_dict() for c in outcome.candidates] + [chosen.to_dict()],
        )
        self.store.log(order_id, f"下载源：{chosen.url}（{outcome.describe()}）")

        # ── 3. 下载 ────────────────────────────────────────────────
        self.store.update(order_id, status=OrderStatus.DOWNLOADING.value,
                          stage="开始下载")
        job_dir = job_dir_for(self.download_dir, order_id)

        last_store_write = [0.0]
        last_report = [-1e9]
        reports_sent = [0]
        dl_started = [time.time()]
        # 买家不想被「进度 1%」刷屏：小文件（秒级下完）干脆一条都不推
        min_seconds = float(self.config.get("replies.progress_min_seconds", 20) or 0)
        max_reports = int(self.config.get("replies.progress_max_count", 3) or 0)

        def on_progress(p: Progress) -> None:
            now = time.time()
            if now - last_store_write[0] >= STORE_PROGRESS_INTERVAL:
                last_store_write[0] = now
                self.store.update(order_id, progress=round(p.percent, 1),
                                  stage=p.stage or "下载中")

            if p.percent <= 0 or reports_sent[0] >= max_reports:
                return
            if now - dl_started[0] < min_seconds:
                return
            if not self.replies.should_report_progress(p.percent, last_report[0]):
                return
            last_report[0] = p.percent
            reports_sent[0] += 1
            fresh = self.store.get(order_id)
            if fresh:
                fresh.progress = p.percent
                self._emit(fresh, self.replies.progress(fresh, p.percent, p.eta))

        result = self.downloader.download(
            chosen.url, job_dir, kind=chosen.kind, title_hint=title,
            progress=on_progress, cancel=cancel,
        )

        if cancel.is_set():
            self._finish_cancelled(order_id)
            return

        if not result.ok:
            # 兜底重试：有些站点的分享短链域名我们没登记，平台认得出来、
            # 类型却判成了「普通页面」，于是没去展开 —— 拿短链直接下载必然失败。
            # 失败后补一次展开再试，代价只是一次请求。
            retried = self._retry_after_expand(order_id, chosen, job_dir, title,
                                               on_progress, cancel)
            if retried is not None:
                result = retried
            if cancel.is_set():
                self._finish_cancelled(order_id)
                return

        if not result.ok:
            self.store.update(order_id, status=OrderStatus.FAILED.value,
                              stage="下载失败", error=result.error)
            self.store.log(order_id, f"下载失败：{result.error}", level="error")
            fresh = self.store.get(order_id)
            if fresh:
                self._emit(fresh, self.replies.failed(fresh))
            return

        # ── 4. 整理产物 ────────────────────────────────────────────
        self.store.update(order_id, status=OrderStatus.PACKAGING.value,
                          stage="整理文件", progress=100.0)
        # 平台元数据标题常更准，但偶尔是自动生成的占位名
        # （实测：小红书给的是 "XiaoHongShu video #6ab896ff..."），
        # 这时买家分享文案里的标题反而更好，两边挑一下。
        best_title = pick_best_title(result.title, title) or title
        final_path = self._finalize(Path(result.file_path), best_title)
        size = final_path.stat().st_size if final_path.exists() else result.size_bytes

        self.store.update(
            order_id, status=OrderStatus.DONE.value, stage="已完成",
            title=best_title, file_path=str(final_path), file_size=size,
            progress=100.0, error="",
        )
        self.store.log(
            order_id,
            f"完成：{final_path.name}（{size / 1024 / 1024:.1f} MB，"
            f"{result.duration_sec:.1f}s，引擎 {result.engine}）",
        )
        fresh = self.store.get(order_id)
        if fresh:
            self._emit(fresh, self.replies.done(fresh), str(final_path))

    # ── 辅助 ───────────────────────────────────────────────────────
    def _finalize(self, path: Path, title: str) -> Path:
        """按标题重命名产物（可选），失败则保持原样 —— 命名不该让订单失败。"""
        if not self.config.get("download.rename_by_title", True) or not path.exists():
            return path
        title = (title or "").strip()
        # 没有真标题、或标题是自动生成的占位名时别乱改名：
        # 把 sample.mp4 改成 video.mp4 或 "XiaoHongShu video #xxx.mp4" 都是净损失
        if is_generic_title(title):
            return path
        maxlen = int(self.config.get("download.filename_maxlen", 60) or 60)
        clean = sanitize_filename(title, maxlen=maxlen)
        if not clean or is_generic_title(clean):
            return path
        suffix = path.suffix or ".mp4"
        target = path.with_name(f"{clean}{suffix}")
        if target.name == path.name:
            return path
        target = ensure_unique(target)
        try:
            path.rename(target)
            return target
        except OSError:
            return path

    def _retry_after_expand(self, order_id: int, chosen, job_dir: Path, title: str,
                            progress, cancel) -> DownloadResult | None:
        """下载失败后：如果这条链接还没展开过，试着展开一次再下。

        真实情况：有些站点的分享短链域名没有登记（比如 ``z.ixigua.com``），
        平台能被识别出来，但类型判成了「普通页面」，于是跳过了展开这一步，
        结果拿一个短链去下载，必然失败。补一次展开重试，代价只是一次请求。
        """
        if chosen.source == "expanded":
            return None
        try:
            exp = self.resolver.expand(chosen.url)
        except Exception as exc:  # noqa: BLE001 - 兜底路径，不能反过来把订单搞挂
            self.store.log(order_id, f"兜底展开异常：{type(exc).__name__}: {exc}",
                           level="warn")
            return None
        if exp.error or not exp.changed:
            return None

        new = self.resolver.recandidate(exp.final_url, "expanded", exp.title, base=0.90)
        self.store.log(
            order_id,
            f"下载失败，但这条链接展开后是另一个地址，重试一次：{snippet(new.url, 80)}",
            level="warn",
        )
        self.store.update(order_id, chosen_url=new.url)
        return self.downloader.download(new.url, job_dir, kind=new.kind,
                                        title_hint=title, progress=progress,
                                        cancel=cancel)

    def _finish_cancelled(self, order_id: int) -> None:
        self.store.update(order_id, status=OrderStatus.CANCELLED.value,
                          stage="已取消", error="任务已取消")
        self.store.log(order_id, "任务被取消", level="warn")

    # ── 控制台用的快照 ─────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "workers": self.workers,
            "pending": self.pending,
            "stats": self.store.stats(),
            "engines": self.downloader.available_engines(),
            "search": self.resolver.router.describe(),
            "download_dir": str(self.download_dir),
        }
