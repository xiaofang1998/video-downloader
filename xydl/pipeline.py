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
from . import series as series_mod
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

#: 塞进队列用来立刻唤醒卡在 get() 上的 worker。
#: worker 用 ``queue.get(timeout=0.5)`` 轮询等待，所以不发这个信号的话，
#: stop() 里每个 worker 都要白等最多半秒才退出 —— 单实例无所谓，
#: 但每个用例都起停一次（测试）就是几十秒。
WAKE = -1


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
        # 先叫醒 worker：它们正卡在 queue.get(timeout=0.5) 上，
        # 不叫的话每个都要白等半秒。
        for _ in self._threads:
            self._queue.put(WAKE)
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
            meta=message.meta,
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
                    auto_reply: bool = True,
                    meta: dict | None = None) -> tuple[Order, bool]:
        """便捷入口：直接给一段文本。"""
        message = IncomingMessage(
            channel=channel, conversation_id=conversation_id, text=text, sender=sender,
            meta=dict(meta or {}),
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
        """取消订单：正在跑就置位取消标志，还没跑就直接改状态。

        **必须先判订单状态、再看取消标志**。worker 收尾时是"先写终态、再回收标志"
        （见 ``_run`` 的 finally），中间那一瞬间标志还在。反过来的顺序会把**已完成**
        的订单报成"取消成功"——运营以为发出去的取消了，其实文件早就交付了。
        这个顺序问题在整套测试里表现为偶发失败，抓到时一度以为是测试写错了。
        """
        order = self.store.get(order_id)
        if order is None or OrderStatus(order.status).is_terminal:
            with self._lock:
                self._cancel_flags.pop(order_id, None)     # 顺手清掉残留标志
            return False

        with self._lock:
            flag = self._cancel_flags.get(order_id)
        if flag is not None:
            flag.set()
            self.store.log(order_id, "收到取消请求", level="warn")
            return True

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
                if order_id == WAKE:
                    # 收到停止信号。回到 while 判断，_running 已是 False 就退出。
                    # 队列里可能还有真订单，但那是「进程正在关」的场景，
                    # 订单状态已经落库，下次启动能重新捞。
                    continue
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
        # 先判断这条链接是「一个视频」还是「一部剧/一个合集」。
        # B站的分P/合集/番剧/UP主投稿都会被展开；视频号要靠 TikHub 才列得出来。
        plan = self._plan_series(order_id, chosen, title)
        if plan.error:
            # 配置层面的错误（比如 episode_range 写得解析不出来）：宁可停下让人
            # 来看，也不能"照旧把全部 50 集都下了" —— 那是在放大损失。
            self.store.update(order_id, status=OrderStatus.NEED_MANUAL.value,
                              stage="合集配置有误", error=plan.error)
            self.store.log(order_id, f"多剧集配置有误，已停止：{plan.error}",
                           level="error")
            fresh = self.store.get(order_id)
            if fresh:
                self._emit(fresh, self.replies.need_manual(fresh, plan.error))
            return
        single_ep = plan.episodes[0] if plan.episodes else None
        if plan.is_series or (plan.expands_series and single_ep is not None
                              and not single_ep.url):
            # 多集，或者"展开后只剩 1 集但那一集只能靠 id 定位"（视频号）：
            # 都走逐集下载流程（1 集时不会打包，直接当单文件交付）。
            self._run_series(order_id, plan, cancel)
            return

        # 展开后只选中 1 集时，要下的是**那一集**，不是买家发来的原始链接。
        # 例：合集链接 + episode_range=5 → 必须下第 5 集；直接拿合集链接去下
        # （命令里带 --no-playlist）会变成第 1 集 —— 下错了集还报成功。
        chosen_url = chosen.url
        if plan.expands_series and single_ep is not None and single_ep.url:
            chosen_url = single_ep.url
            if single_ep.title:
                title = single_ep.title
            self.store.update(order_id, chosen_url=chosen_url)
            self.store.log(
                order_id,
                f"合集里只选中 1 集，实际下载：{snippet(chosen_url, 90)}",
            )

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
            chosen_url, job_dir, kind=chosen.kind, title_hint=title,
            progress=on_progress, cancel=cancel,
        )

        if cancel.is_set():
            self._finish_cancelled(order_id)
            return

        if not result.ok and chosen_url == chosen.url:
            # 兜底重试：有些站点的分享短链域名我们没登记，平台认得出来、
            # 类型却判成了「普通页面」，于是没去展开 —— 拿短链直接下载必然失败。
            # 失败后补一次展开再试，代价只是一次请求。
            # （已经换成"合集里那一集"的地址时不走这条：那条链接是展开出来的，
            #   再拿原始链接去展开重试只会下错集。）
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

    # ── 多剧集 ─────────────────────────────────────────────────────
    def _plan_series(self, order_id: int, chosen, title: str
                     ) -> "series_mod.SeriesPlan":
        """判断这条链接对应几集。任何异常都退化成单集，不让订单因此挂掉。"""
        def log(message: str) -> None:
            self.store.log(order_id, message)

        # 每条订单可以带自己的选集（界面上的"合集可勾选"）：放进订单 meta，
        # 这里用一个**配置副本**覆盖，不动全局配置（否则并发订单会互相串）。
        config = self.config
        order = self.store.get(order_id)
        picked = ""
        if order is not None and isinstance(order.meta, dict):
            picked = str(order.meta.get("episode_range") or "").strip()
        if picked:
            config = self.config.copy()
            config.set("download.series.episode_range", picked)
            self.store.log(order_id, f"本单选集：{picked}")

        try:
            plan = series_mod.plan(
                chosen.url,
                config=config,
                engine=self.downloader.ytdlp,
                title_hint=title,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001 - 展开失败只该退化成单集
            self.store.log(order_id, f"合集展开异常，按单集处理："
                                     f"{type(exc).__name__}: {exc}", level="warn")
            return series_mod.SeriesPlan(
                title=title,
                episodes=[series_mod.Episode(index=1, url=chosen.url, title=title)],
                source="single",
            )
        if plan.is_series:
            self.store.log(order_id, f"识别为多剧集：{plan.describe()}")
        elif plan.note:
            self.store.log(order_id, f"多剧集：{plan.note}")
        return plan

    def _run_series(self, order_id: int, plan: "series_mod.SeriesPlan",
                    cancel: threading.Event) -> None:
        """逐集下载 → 打包 → 落单。

        刻意**不引入新的订单状态**：一次合集仍然是一笔订单（客户也是按一笔付钱），
        只是产物从「一个文件」变成「一个压缩包」。状态机保持不变，控制台、
        桥接回传、上传通道全都不用改。
        """
        order = self.store.get(order_id)
        if order is None:
            return
        base_meta = dict(order.meta or {})
        title = plan.title or order.title or "合集"
        job_dir = job_dir_for(self.download_dir, order_id)
        episodes_dir = job_dir / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)
        # 开跑前清掉上一轮的逐集产物。与「单订单一个目录」同一个道理：
        # 订单重试时会复用同一个 job_dir，而 _place_episode 走 ensure_unique，
        # 不清理就会堆出 "01 xxx (2).mp4"，zip 里出现重复集。
        # （Downloader._clean_job_dir 只删顶层文件、不递归子目录，所以这里要自己来。）
        self._clean_series_dir(episodes_dir)

        total = plan.count
        files: list[Path] = []
        failures: list[str] = []
        last_store_write = [0.0]
        reports_sent = [0]
        last_report = [-1e9]
        min_seconds = float(self.config.get("replies.progress_min_seconds", 20) or 0)
        max_reports = int(self.config.get("replies.progress_max_count", 3) or 0)
        dl_started = time.time()

        for position, episode in enumerate(plan.episodes, 1):
            if cancel.is_set():
                self._finish_cancelled(order_id)
                return

            stage = f"下载第 {position}/{total} 集"
            self.store.update(order_id, status=OrderStatus.DOWNLOADING.value,
                              stage=stage,
                              progress=round((position - 1) / total * 100, 1))
            episode_dir = episodes_dir / f"{position:02d}"

            def on_progress(p: Progress, _pos=position) -> None:
                now = time.time()
                if now - last_store_write[0] >= STORE_PROGRESS_INTERVAL:
                    last_store_write[0] = now
                    overall = ((_pos - 1) + max(0.0, p.percent) / 100.0) / total * 100
                    self.store.update(order_id, progress=round(overall, 1),
                                      stage=p.stage or f"下载第 {_pos}/{total} 集")
                if p.percent <= 0 or reports_sent[0] >= max_reports:
                    return
                if now - dl_started < min_seconds:
                    return
                if not self.replies.should_report_progress(p.percent, last_report[0]):
                    return
                last_report[0] = p.percent
                reports_sent[0] += 1
                fresh = self.store.get(order_id)
                if fresh:
                    fresh.progress = p.percent
                    self._emit(fresh, self.replies.progress(fresh, p.percent, p.eta))

            result = self._download_episode(episode, episode_dir, plan,
                                            progress=on_progress, cancel=cancel)
            if cancel.is_set():
                self._finish_cancelled(order_id)
                return

            if not result.ok:
                failures.append(f"第{position}集：{result.error}")
                self.store.log(order_id, f"第 {position}/{total} 集下载失败："
                                         f"{result.error}", level="error")
                continue

            named = self._place_episode(result, episode, position, episodes_dir,
                                        series_title=plan.title)
            files.append(named)
            self.store.log(order_id, f"第 {position}/{total} 集完成：{named.name}"
                                     f"（{named.stat().st_size / 1024 / 1024:.1f} MB）")

        if not files:
            error = "；".join(failures[:3]) or "所有集都下载失败"
            self.store.update(order_id, status=OrderStatus.FAILED.value,
                              stage="合集下载失败", error=error)
            fresh = self.store.get(order_id)
            if fresh:
                self._emit(fresh, self.replies.failed(fresh))
            return

        # ── 打包 ───────────────────────────────────────────────────
        mode = str(self.config.get("download.series.mode", "zip") or "zip").lower()
        # 重新读一次 meta 再合并：一批 50 集要跑几十分钟，中途若有别的写入者
        # 给这单补过 meta，整列覆盖（Store.update 是 SET meta=?）会把它抹掉。
        current = self.store.get(order_id)
        meta = dict(current.meta if current else base_meta) or {}
        meta["series"] = {
            "title": title,
            "count": total,                     # 本次计划要下的集数
            "downloaded": len(files),           # 真正下成功的集数
            "failed": failures,
            "source": plan.source,
            "mode": mode,
            "truncated": bool(plan.truncated),  # 是否被 max_episodes 截断
            "files": [p.name for p in files],
        }
        if plan.truncated:
            self.store.log(
                order_id,
                f"注意：这次只下了 {total} 集，已达 download.series.max_episodes "
                f"上限，原列表可能更长", level="warn",
            )

        deliverable = files[0]
        if mode == "zip" and len(files) > 1:
            self.store.update(order_id, status=OrderStatus.PACKAGING.value,
                              stage=f"打包 {len(files)} 集", progress=99.0,
                              meta=meta)
            archive_path, error = series_mod.archive(files, job_dir / f"{title}.zip")
            if archive_path is None:
                self.store.log(order_id, f"打包失败，改为逐文件交付：{error}",
                               level="warn")
            else:
                deliverable = archive_path

        self.store.update(order_id, status=OrderStatus.PACKAGING.value,
                          stage="整理文件", progress=100.0, meta=meta)
        final_path = self._finalize(Path(deliverable), title)
        size = final_path.stat().st_size if final_path.exists() else 0

        error_note = ""
        if failures:
            error_note = (f"{len(failures)}/{total} 集失败：" + "；".join(failures[:2]))
        self.store.update(
            order_id, status=OrderStatus.DONE.value, stage="已完成",
            title=title, file_path=str(final_path), file_size=size,
            progress=100.0, error=error_note,
        )
        self.store.log(
            order_id,
            f"合集完成：{len(files)}/{total} 集，交付 {final_path.name}"
            f"（{size / 1024 / 1024:.1f} MB）"
            + (f"；{error_note}" if error_note else ""),
        )
        fresh = self.store.get(order_id)
        if fresh:
            note = (self.replies.series_truncated_note(total)
                    if plan.truncated else "")
            self._emit(fresh,
                       self.replies.series_done(fresh, len(files), total=total,
                                                failed=len(failures), note=note),
                       str(final_path))

    def _download_episode(self, episode: "series_mod.Episode", episode_dir: Path,
                          plan: "series_mod.SeriesPlan",
                          progress: Callable[[Progress], None],
                          cancel: threading.Event) -> DownloadResult:
        """下一集。视频号走它自己的解析链（按 object_id / export_id 定位）。"""
        hint = episode.hint(plan.title)
        if episode.source == "wechat_channels" and not episode.url:
            return self.downloader.download_channels_episode(
                episode.object_id, episode.export_id, episode_dir,
                title_hint=hint, progress=progress, cancel=cancel)
        return self.downloader.download(
            episode.url, episode_dir, kind="page", title_hint=hint,
            progress=progress, cancel=cancel)

    @staticmethod
    def _clean_series_dir(episodes_dir: Path) -> None:
        """清空合集目录里的上一轮产物（文件与逐集子目录一起，否则会堆重复集）。"""
        import shutil

        try:
            items = list(episodes_dir.iterdir())
        except OSError:
            return
        for item in items:
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
            except OSError:
                pass

    @staticmethod
    def _place_episode(result: DownloadResult, episode: "series_mod.Episode",
                       position: int, episodes_dir: Path,
                       series_title: str = "") -> Path:
        """把某一集的产物搬到 ``episodes/`` 并改成 ``01 标题.mp4``。

        编号是必须的：客户拿到一堆积名字相似的 mp4，没有序号根本排不出顺序。
        标题也要"去冗余"：yt-dlp 给多分P的标题是「合集名 p01 分P名」，
        而分P名常常就是合集名，原样落盘会得到 ``01 某某 p01 某某.mp4``。

        编号用**原始集号**而不是本次循环序号：运营用 ``episode_range=9,10``
        只下第 9、10 集时，文件名得是 ``09/10`` —— 写成 ``01/02`` 客户会以为
        收到的是前两集。
        """
        source = Path(result.file_path)
        suffix = source.suffix or ".mp4"
        number = episode.index if episode.index and episode.index > 0 else position
        title = (series_mod.clean_episode_title(episode.title, series_title)
                 or series_mod.clean_episode_title(result.title, series_title)
                 or f"第{number}集")
        target = episodes_dir / series_mod.episode_filename(number, title, suffix)
        target = ensure_unique(target)
        try:
            if source.parent != episodes_dir:
                source.replace(target)
            else:
                return source
            return target
        except OSError:
            return source

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
