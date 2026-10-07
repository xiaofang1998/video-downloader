"""目录收件箱渠道的测试。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from xydl.channels.inbox import InboxChannel
from xydl.config import Config
from xydl.models import IncomingMessage
from xydl.pipeline import Pipeline
from xydl.store import Store
from conftest import SAMPLE_BYTES


# ── 解析 ──────────────────────────────────────────────────────────────
def test_parse_txt_with_metadata(tmp_path: Path):
    path = tmp_path / "m1.txt"
    path.write_text(
        "conversation: buyer789\n"
        "sender: 王五\n"
        "---\n"
        "https://v.douyin.com/abc/ 帮我下这个\n",
        encoding="utf-8",
    )
    msg = InboxChannel.parse_file(path)
    assert isinstance(msg, IncomingMessage)
    assert msg.conversation_id == "buyer789"
    assert msg.sender == "王五"
    assert msg.msg_id == "m1"
    assert "v.douyin.com" in msg.text
    assert "conversation:" not in msg.text      # 元信息不能混进正文


def test_parse_txt_without_metadata_treats_everything_as_text(tmp_path: Path):
    path = tmp_path / "m2.txt"
    path.write_text("https://www.bilibili.com/video/BV1xx\n", encoding="utf-8")
    msg = InboxChannel.parse_file(path)
    assert msg.conversation_id == "inbox"
    assert msg.text.startswith("https://")


def test_parse_json(tmp_path: Path):
    path = tmp_path / "m3.json"
    path.write_text(json.dumps({"conversation_id": "b1", "sender": "赵六",
                                "text": "https://youtu.be/x", "msg_id": "custom-9"}),
                    encoding="utf-8")
    msg = InboxChannel.parse_file(path)
    assert msg.conversation_id == "b1"
    assert msg.msg_id == "custom-9"


def test_parse_json_missing_text_raises(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"conversation_id": "b1"}), encoding="utf-8")
    with pytest.raises(ValueError, match="text"):
        InboxChannel.parse_file(path)


def test_parse_empty_txt_returns_none(tmp_path: Path):
    path = tmp_path / "empty.txt"
    path.write_text("   \n\n", encoding="utf-8")
    assert InboxChannel.parse_file(path) is None


def test_parse_bad_json_raises(tmp_path: Path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        InboxChannel.parse_file(path)


# ── 端到端 ────────────────────────────────────────────────────────────
@pytest.fixture
def inbox(config: Config, store: Store):
    config.set("channels.inbox.enabled", True)
    config.set("channels.inbox.poll_sec", 0.2)
    pipeline = Pipeline(config, store)
    pipeline.start()
    channel = InboxChannel(pipeline, config)
    channel.start()
    yield channel, pipeline, config.path_of("inbox_dir")
    channel.stop()
    pipeline.stop()


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("等待条件超时")


def drop(path: Path, content: str) -> None:
    """写文件并把 mtime 拨回过去，避免撞上「可能还在写」的保护窗口。"""
    path.write_text(content, encoding="utf-8")
    old = time.time() - 10
    os.utime(path, (old, old))


def test_message_file_is_processed_and_moved(inbox, store, sample_server):
    channel, pipeline, root = inbox
    src = root / "order1.txt"
    drop(src, f"conversation: buyer_alpha\nsender: 张三\n---\n"
              f"{sample_server.url('sample.mp4')}\n")

    assert wait_for(lambda: (root / "processed" / "order1.txt").exists())
    assert not src.exists()

    order = wait_for(lambda: (store.list_orders(search="buyer_alpha") or [None])[0])
    from xydl.models import OrderStatus
    final = wait_for(lambda: (lambda o: o if o and OrderStatus(o.status).is_terminal
                              else None)(store.get(order.id)))
    assert final.status == OrderStatus.DONE.value
    assert Path(final.file_path).read_bytes() == SAMPLE_BYTES


def test_reply_is_written_next_to_the_input(inbox, store, sample_server):
    channel, pipeline, root = inbox
    drop(root / "order2.txt",
         f"conversation: buyer_beta\n---\n{sample_server.url('sample.mp4')}\n")

    reply_file = root / "processed" / "order2.reply.txt"
    assert wait_for(lambda: reply_file.exists() and "下载好啦" in
                    reply_file.read_text(encoding="utf-8"))

    content = reply_file.read_text(encoding="utf-8")
    assert "订单" in content
    assert "文件：" in content


def test_json_message_is_processed(inbox, store, sample_server):
    channel, pipeline, root = inbox
    drop(root / "order3.json",
         json.dumps({"conversation_id": "buyer_gamma",
                     "text": sample_server.url("sample.mp4")}))

    assert wait_for(lambda: (root / "processed" / "order3.json").exists())
    assert wait_for(lambda: bool(store.list_orders(search="buyer_gamma")))


def test_broken_file_is_quarantined_with_a_reason(inbox, store):
    channel, pipeline, root = inbox
    drop(root / "bad.json", "{not json at all")

    assert wait_for(lambda: (root / "failed" / "bad.json").exists())
    note = root / "failed" / "bad.json.error.txt"
    assert wait_for(lambda: note.exists())
    assert "解析失败" in note.read_text(encoding="utf-8")
    assert not (root / "processed" / "bad.json").exists()


def test_same_filename_is_not_processed_twice(inbox, store, sample_server):
    """外部程序重复投递同一文件名（重试逻辑写错时很常见）不该重复下载。"""
    channel, pipeline, root = inbox
    drop(root / "dup.txt", f"conversation: buyer_delta\n---\n"
                           f"{sample_server.url('sample.mp4')}\n")
    assert wait_for(lambda: (root / "processed" / "dup.txt").exists())

    # 再丢一份同名（但内容不同）——msg_id 相同，应被识别为重复
    drop(root / "dup.txt", f"conversation: buyer_delta\n---\n"
                           f"{sample_server.url('clip.webm')}\n")
    assert wait_for(lambda: (root / "processed" / "dup.txt").exists())
    time.sleep(0.5)

    orders = store.list_orders(search="buyer_delta")
    assert len(orders) == 1
    assert "重复" in (root / "processed" / "dup.reply.txt").read_text(encoding="utf-8")
