"""流水线端到端测试 —— 用本地 HTTP 服务器，完全离线、结果确定。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xydl.config import Config
from xydl.models import IncomingMessage, OrderStatus
from xydl.pipeline import Pipeline
from xydl.store import Store
from conftest import SAMPLE_BYTES, wait_for_terminal


@pytest.fixture
def pipeline(config: Config, store: Store):
    p = Pipeline(config, store)
    p.start()
    yield p
    p.stop()


def collect(pipeline: Pipeline) -> list[tuple[str, str, str]]:
    got: list[tuple[str, str, str]] = []
    pipeline.add_deliverer(lambda o, t, f: got.append((o.status, t, f)))
    return got


# ── 正常路径 ──────────────────────────────────────────────────────────
def test_full_success_flow(config, store, sample_server, pipeline):
    replies = collect(pipeline)
    order, created = pipeline.submit_text(sample_server.url("sample.mp4"),
                                          conversation_id="buyer1", sender="张三")
    assert created

    final = wait_for_terminal(store, order.id)
    assert final.status == OrderStatus.DONE.value, final.error
    assert Path(final.file_path).read_bytes() == SAMPLE_BYTES
    assert final.file_size == len(SAMPLE_BYTES)

    texts = [t for _, t, _ in replies]
    assert any("链接收到" in t for t in texts), "应立刻自动回一条「已接单」"
    assert any("下载好啦" in t for t in texts), "完成后应回一条交付话术"
    assert any(f for _, _, f in replies), "交付时应带上文件路径"


def test_order_records_a_full_event_trail(config, store, sample_server, pipeline):
    order, _ = pipeline.submit_text(sample_server.url("sample.mp4"))
    wait_for_terminal(store, order.id)
    trail = " ".join(e["message"] for e in store.events(order.id))
    assert "接单" in trail
    assert "识别到" in trail
    assert "下载源" in trail
    assert "完成" in trail


def test_file_lands_in_per_order_directory(config, store, sample_server, pipeline):
    order, _ = pipeline.submit_text(sample_server.url("sample.mp4"))
    final = wait_for_terminal(store, order.id)
    assert Path(final.file_path).parent.name == str(order.id)
    assert Path(final.file_path).parent.parent == pipeline.download_dir


def test_direct_link_keeps_its_own_filename(config, store, sample_server, pipeline):
    """直链没标题时不该被改名成 video.mp4 —— 原名更有信息量。"""
    order, _ = pipeline.submit_text(sample_server.url("sample.mp4"))
    final = wait_for_terminal(store, order.id)
    assert Path(final.file_path).name == "sample.mp4"


# ── 幂等 ──────────────────────────────────────────────────────────────
def test_duplicate_message_is_ignored(config, store, sample_server, pipeline):
    msg = IncomingMessage(channel="console", conversation_id="b1",
                          text=sample_server.url("sample.mp4"), msg_id="fixed-1")
    first, created1 = pipeline.submit(msg)
    second, created2 = pipeline.submit(msg)
    assert created1 and not created2
    assert first.id == second.id


def test_duplicate_message_greets_only_once(config, store, sample_server, pipeline):
    """重复投递不该再给买家发一遍「已接单」。"""
    replies = collect(pipeline)
    msg = IncomingMessage(channel="console", conversation_id="b1",
                          text=sample_server.url("sample.mp4"), msg_id="dup-1")
    pipeline.submit(msg)
    pipeline.submit(msg)
    assert len([t for _, t, _ in replies if "链接收到" in t]) == 1


# ── 人工兜底路径 ──────────────────────────────────────────────────────
def test_drm_platform_goes_to_manual_without_downloading(config, store, pipeline):
    replies = collect(pipeline)
    order, _ = pipeline.submit_text(
        "https://v.qq.com/x/cover/abc.html 这个电影能下吗", conversation_id="b2"
    )
    final = wait_for_terminal(store, order.id)
    assert final.status == OrderStatus.NEED_MANUAL.value
    assert not final.file_path
    assert any("版权" in t for t in (t for _, t, _ in replies))


def test_message_without_any_link_goes_to_manual(config, store, pipeline):
    replies = collect(pipeline)
    order, _ = pipeline.submit_text("在吗", conversation_id="b3")
    final = wait_for_terminal(store, order.id)
    assert final.status == OrderStatus.NEED_MANUAL.value
    assert any("暂时没解析出来" in t for t in (t for _, t, _ in replies))


def test_html_page_is_reported_as_failed(config, store, sample_server, pipeline):
    replies = collect(pipeline)
    order, _ = pipeline.submit_text(sample_server.url("page.html"), conversation_id="b4")
    final = wait_for_terminal(store, order.id)
    assert final.status == OrderStatus.FAILED.value
    assert "网页" in final.error
    assert any("下载失败" in t for t in (t for _, t, _ in replies))


def test_404_is_reported_as_failed(config, store, sample_server, pipeline):
    order, _ = pipeline.submit_text(sample_server.url("missing.mp4"),
                                    conversation_id="b5")
    final = wait_for_terminal(store, order.id)
    assert final.status == OrderStatus.FAILED.value
    assert final.error


# ── 重试与取消 ────────────────────────────────────────────────────────
def test_retry_requeues_a_failed_order(config, store, sample_server, pipeline):
    order, _ = pipeline.submit_text(sample_server.url("missing.mp4"))
    wait_for_terminal(store, order.id)
    assert pipeline.retry(order.id) is True
    again = wait_for_terminal(store, order.id)
    assert again.status == OrderStatus.FAILED.value


def test_retry_refuses_orders_still_in_flight(config, store, sample_server, pipeline):
    order, _ = pipeline.submit_text(sample_server.url("sample.mp4"))
    store.update(order.id, status=OrderStatus.DOWNLOADING.value)
    assert pipeline.retry(order.id) is False
    assert pipeline.retry(999999) is False


def test_cancel_on_finished_order_is_a_noop(config, store, sample_server, pipeline):
    order, _ = pipeline.submit_text(sample_server.url("sample.mp4"))
    wait_for_terminal(store, order.id)
    assert pipeline.cancel(order.id) is False      # 已终态，取消不了
    assert pipeline.cancel(999999) is False


def test_cancel_moves_pending_order_to_cancelled(config, store, pipeline):
    """还没被 worker 捡走的订单，取消应直接改状态。"""
    order, _ = store.create_order("console:manual-1", "console", "b9", "https://x/y")
    assert pipeline.cancel(order.id) is True
    assert store.get(order.id).status == OrderStatus.CANCELLED.value


# ── 容错 ──────────────────────────────────────────────────────────────
def test_deliverer_exception_does_not_break_the_order(config, store, sample_server,
                                                      pipeline):
    def bad_deliverer(order, text, file_path):
        raise RuntimeError("渠道炸了")

    pipeline.add_deliverer(bad_deliverer)
    order, _ = pipeline.submit_text(sample_server.url("sample.mp4"))
    final = wait_for_terminal(store, order.id)
    assert final.status == OrderStatus.DONE.value
    trail = " ".join(e["message"] for e in store.events(order.id))
    assert "投递失败" in trail


def test_reply_is_in_outbox_even_with_bad_deliverer(config, store, sample_server,
                                                     pipeline):
    pipeline.add_deliverer(lambda o, t, f: (_ for _ in ()).throw(RuntimeError("x")))
    order, _ = pipeline.submit_text(sample_server.url("sample.mp4"))
    wait_for_terminal(store, order.id)
    assert len(store.pop_outbox()) >= 2       # 至少「已接单」+「已完成」


def test_snapshot_shape(config, store, pipeline):
    snap = pipeline.snapshot()
    assert snap["running"] is True
    assert snap["workers"] == 1
    for key in ("stats", "engines", "search", "download_dir", "pending"):
        assert key in snap


# ── 失败后展开重试 ────────────────────────────────────────────────────
def test_retry_after_expand_recovers_unregistered_short_domain(config, store,
                                                               monkeypatch):
    """有些站点的分享短链域名没登记，平台认得出但类型判成「普通页面」，
    于是跳过了展开 —— 拿短链直接下载必然失败。失败后应补一次展开重试。

    真实案例：西瓜的 z.ixigua.com/yvkw 就是这种。
    """
    from xydl.downloader import DownloadResult
    from xydl.models import LinkCandidate
    from xydl.resolver import ExpandResult

    p = Pipeline(config, store)
    tried: list[str] = []

    monkeypatch.setattr(
        p.resolver, "expand",
        lambda url: ExpandResult(
            original=url,
            final_url="https://www.ixigua.com/7415953275592704521",
            chain=[url, "https://www.ixigua.com/7415953275592704521"],
            status=200),
    )
    monkeypatch.setattr(
        p.downloader, "download",
        lambda url, job, **kw: (tried.append(url),
                                DownloadResult(False, error="测试里不真下"))[1],
    )

    cand = LinkCandidate(url="https://z.unknown-short.example/yvkw",
                         platform="unknown", kind="page", source="message")
    result = p._retry_after_expand(1, cand, config.path_of("download_dir"),
                                   "标题", lambda _p: None, None)

    assert result is not None
    assert tried == ["https://www.ixigua.com/7415953275592704521"]   # 确实用展开后的地址重试了


def test_retry_after_expand_gives_up_when_url_does_not_redirect(config, store,
                                                                monkeypatch):
    from xydl.downloader import DownloadResult
    from xydl.models import LinkCandidate
    from xydl.resolver import ExpandResult

    p = Pipeline(config, store)
    tried: list[str] = []
    monkeypatch.setattr(p.resolver, "expand",
                        lambda url: ExpandResult(original=url, final_url=url,
                                                 chain=[url], status=200))
    monkeypatch.setattr(p.downloader, "download",
                        lambda url, job, **kw: tried.append(url))

    cand = LinkCandidate(url="https://x.example/a", platform="unknown",
                         kind="page", source="message")
    assert p._retry_after_expand(1, cand, config.path_of("download_dir"),
                                 "t", lambda _p: None, None) is None
    assert tried == []          # 没跳转就不该白重试一次


def test_retry_after_expand_skips_already_expanded(config, store, monkeypatch):
    from xydl.models import LinkCandidate

    p = Pipeline(config, store)
    called: list[str] = []
    monkeypatch.setattr(p.resolver, "expand", lambda url: called.append(url))

    cand = LinkCandidate(url="https://v.douyin.com/x/", platform="douyin",
                         kind="short", source="expanded")
    assert p._retry_after_expand(1, cand, config.path_of("download_dir"),
                                 "t", lambda _p: None, None) is None
    assert called == []         # 已经展开过就不再展开
