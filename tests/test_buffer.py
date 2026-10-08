"""连续发送缓冲单测：攒内容、上限、超时、失败回填"""

import asyncio
import json

import pytest

from src.buffer import BufferFull, BufferStore, SessionBuffer


def test_session_buffer_counts():
    buf = SessionBuffer(max_items=10)
    assert buf.empty

    buf.add("text", "第一段")
    buf.add("image", "https://blog/u/1.jpg")
    buf.add("text", "第二段")

    assert buf.count == 3
    assert buf.text_count == 2
    assert buf.image_count == 1
    assert not buf.empty


def test_session_buffer_json_shape():
    buf = SessionBuffer()
    buf.add("text", "你好")
    buf.add("image", "https://blog/u/1.jpg")

    assert json.loads(buf.build()) == {
        "results": [
            {"type": "text", "content": "你好"},
            {"type": "image", "content": "https://blog/u/1.jpg"},
        ]
    }


def test_session_buffer_enforces_limit():
    buf = SessionBuffer(max_items=2)
    buf.add("text", "a")
    buf.add("text", "b")
    with pytest.raises(BufferFull) as exc:
        buf.add("text", "c")
    assert "最多攒 2 条" in str(exc.value)


def test_items_returns_a_copy():
    buf = SessionBuffer()
    buf.add("text", "a")
    buf.items.append(("text", "偷偷加的"))
    assert buf.count == 1


async def test_store_start_and_close():
    store = BufferStore(timeout=60)
    assert not store.is_active("s1")

    store.start("s1")
    assert store.is_active("s1")
    store.add("s1", "text", "hi")
    assert store.get("s1").count == 1

    buf = store.close("s1")
    assert buf.count == 1
    assert not store.is_active("s1")
    await store.shutdown()


async def test_store_add_requires_active_session():
    store = BufferStore(timeout=60)
    with pytest.raises(KeyError):
        store.add("s1", "text", "hi")
    await store.shutdown()


async def test_store_enforces_limit():
    store = BufferStore(timeout=60, max_items=2)
    store.start("s1")
    store.add("s1", "text", "a")
    store.add("s1", "text", "b")
    with pytest.raises(BufferFull):
        store.add("s1", "text", "c")
    await store.shutdown()


async def test_restore_puts_buffer_back():
    store = BufferStore(timeout=60)
    store.start("s1")
    store.add("s1", "text", "别丢了我")

    buf = store.close("s1")
    store.restore("s1", buf)

    assert store.is_active("s1")
    assert store.get("s1").count == 1
    await store.shutdown()


async def test_timeout_drops_buffer_and_notifies():
    fired = []

    async def on_timeout(session_id, buf):
        fired.append((session_id, buf.count))

    store = BufferStore(timeout=0.05, on_timeout=on_timeout)
    store.start("s1")
    store.add("s1", "text", "会超时")

    await asyncio.sleep(0.25)

    assert fired == [("s1", 1)]
    assert not store.is_active("s1")
    await store.shutdown()


async def test_each_add_resets_the_idle_timer():
    fired = []

    async def on_timeout(session_id, buf):
        fired.append(session_id)

    store = BufferStore(timeout=0.15, on_timeout=on_timeout)
    store.start("s1")

    for _ in range(4):
        await asyncio.sleep(0.08)
        store.add("s1", "text", "还在发")

    assert fired == []  # 一直在发就不该超时
    await asyncio.sleep(0.3)
    assert fired == ["s1"]  # 停下来了才超时
    await store.shutdown()


async def test_timeout_callback_error_does_not_break_store():
    async def on_timeout(session_id, buf):
        raise RuntimeError("回调炸了")

    store = BufferStore(timeout=0.05, on_timeout=on_timeout)
    store.start("s1")
    await asyncio.sleep(0.2)

    assert not store.is_active("s1")
    await store.shutdown()


async def test_sessions_are_isolated():
    store = BufferStore(timeout=60)
    store.start("群A")
    store.start("群B")
    store.add("群A", "text", "A 的内容")

    assert store.get("群A").count == 1
    assert store.get("群B").count == 0

    store.close("群A")
    assert store.is_active("群B")
    await store.shutdown()
