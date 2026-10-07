"""订单存储层的测试。"""

from __future__ import annotations

import threading

from xydl.models import OrderStatus
from xydl.store import Store


def test_create_and_get(store: Store):
    order, created = store.create_order("c:1", "console", "buyer1", "你好", sender="张三")
    assert created
    assert order.id
    assert order.status == OrderStatus.RECEIVED.value
    assert store.get(order.id).conversation_id == "buyer1"


def test_create_is_idempotent_by_external_id(store: Store):
    first, created1 = store.create_order("c:2", "console", "b", "hi")
    second, created2 = store.create_order("c:2", "console", "b", "hi")
    assert created1 is True
    assert created2 is False
    assert first.id == second.id


def test_create_writes_an_intake_event(store: Store):
    order, _ = store.create_order("c:3", "console", "b", "hi")
    messages = [e["message"] for e in store.events(order.id)]
    assert any("接单" in m for m in messages)


def test_update_only_touches_known_columns(store: Store):
    order, _ = store.create_order("c:4", "console", "b", "hi")
    store.update(order.id, status=OrderStatus.DOWNLOADING.value, progress=12.5,
                 bogus_column="nope")   # 不认识的字段应被忽略而不是报错
    refreshed = store.get(order.id)
    assert refreshed.status == OrderStatus.DOWNLOADING.value
    assert abs(refreshed.progress - 12.5) < 1e-6


def test_update_serializes_links(store: Store):
    order, _ = store.create_order("c:5", "console", "b", "hi")
    store.update(order.id, links=[{"url": "https://x/y", "platform": "douyin"}])
    assert store.get(order.id).links[0]["platform"] == "douyin"


def test_links_survive_round_trip_with_corrupt_json(store: Store):
    order, _ = store.create_order("c:6", "console", "b", "hi")
    store.conn.execute("UPDATE orders SET links='{ not json' WHERE id=?", (order.id,))
    assert store.get(order.id).links == []      # 坏数据不该让整单读不出来


def test_outbox_is_delivered_exactly_once(store: Store):
    order, _ = store.create_order("c:7", "console", "b", "hi")
    store.push_outbox(order.id, "b", "第一条")
    store.push_outbox(order.id, "b", "第二条")
    first = store.pop_outbox()
    assert [m["text"] for m in first] == ["第一条", "第二条"]
    assert store.pop_outbox() == []


def test_mark_replied(store: Store):
    order, _ = store.create_order("c:8", "console", "b", "hi")
    store.mark_replied(order.id)
    row = store.conn.execute("SELECT replied_at FROM orders WHERE id=?",
                             (order.id,)).fetchone()
    assert row["replied_at"] is not None


def test_list_orders_filters(store: Store):
    a, _ = store.create_order("c:9a", "console", "alice", "猫咪视频")
    store.create_order("c:9b", "inbox", "bob", "狗狗视频")
    store.update(a.id, status=OrderStatus.DONE.value)

    assert len(store.list_orders(status=OrderStatus.DONE.value)) == 1
    assert len(store.list_orders(channel="inbox")) == 1
    assert len(store.list_orders(search="狗狗")) == 1
    assert len(store.list_orders(search="不存在的东西")) == 0


def test_list_orders_active_excludes_terminal(store: Store):
    a, _ = store.create_order("c:10a", "console", "a", "x")
    b, _ = store.create_order("c:10b", "console", "b", "y")
    for status in (OrderStatus.DONE, OrderStatus.FAILED, OrderStatus.CANCELLED):
        store.update(a.id, status=status.value)
        assert a.id not in [o.id for o in store.list_orders(status="active")]
    store.update(b.id, status=OrderStatus.DOWNLOADING.value)
    assert b.id in [o.id for o in store.list_orders(status="active")]


def test_stats(store: Store):
    a, _ = store.create_order("c:11a", "console", "a", "x")
    store.create_order("c:11b", "console", "b", "y")
    store.update(a.id, status=OrderStatus.DONE.value)
    stats = store.stats()
    assert stats["total"] == 2
    assert stats["done"] == 1
    assert stats["active"] == 1


def test_concurrent_writes_do_not_corrupt(tmp_path):
    """控制台线程和 worker 线程会同时写，WAL 模式必须扛得住。"""
    st = Store(tmp_path / "concurrent.sqlite3")
    errors: list[Exception] = []

    def worker(tag: str) -> None:
        try:
            for i in range(25):
                order, _ = st.create_order(f"{tag}:{i}", "console", tag, f"msg {i}")
                st.update(order.id, progress=float(i))
                st.log(order.id, f"{tag} 处理 {i}")
                st.push_outbox(order.id, tag, f"reply {i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert st.stats()["total"] == 150
    assert len(st.pop_outbox(limit=1000)) == 150
    st.close()
