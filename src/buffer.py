"""连续发送缓冲 —— 把多条消息攒成一条 mixed_talk

内容按顺序存在内存列表里直接产出 JSON，**不学微信版**用 `@` / `->` 拼成字符串
再切开（那样内容里只要出现 `@` 或 `->` 就会解析错位）。
"""

import asyncio
import logging
from typing import Awaitable, Callable

from .content import build_mixed, summarize

logger = logging.getLogger("astrbot")

ITEM_TEXT = "text"
ITEM_IMAGE = "image"


class BufferFull(Exception):
    """超过一轮能攒的条数上限"""


class SessionBuffer:
    """单个会话的缓冲，item 为 (type, content)"""

    def __init__(self, max_items: int = 50) -> None:
        self._items: list[tuple[str, str]] = []
        self._max_items = max_items

    @property
    def items(self) -> list[tuple[str, str]]:
        return list(self._items)

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def empty(self) -> bool:
        return not self._items

    @property
    def text_count(self) -> int:
        return sum(1 for t, _ in self._items if t == ITEM_TEXT)

    @property
    def image_count(self) -> int:
        return sum(1 for t, _ in self._items if t == ITEM_IMAGE)

    def add(self, item_type: str, content: str) -> int:
        if len(self._items) >= self._max_items:
            raise BufferFull(
                f"一轮最多攒 {self._max_items} 条，先 end 发送或 cancel 取消吧"
            )
        self._items.append((item_type, content))
        return len(self._items)

    def build(self) -> str:
        return build_mixed(self._items)

    def preview(self, limit: int = 20) -> str:
        return summarize(self._items, limit)


class BufferStore:
    """多会话缓冲管理 + 空闲超时自动放弃"""

    def __init__(
        self,
        timeout: int = 300,
        max_items: int = 50,
        on_timeout: Callable[[str, SessionBuffer], Awaitable[None]] | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_items = max_items
        self.on_timeout = on_timeout
        self._buffers: dict[str, SessionBuffer] = {}
        self._timers: dict[str, asyncio.Task] = {}

    # ── 查询 ──────────────────────────────────────────────

    def is_active(self, session_id: str) -> bool:
        return session_id in self._buffers

    def get(self, session_id: str) -> SessionBuffer | None:
        return self._buffers.get(session_id)

    # ── 生命周期 ──────────────────────────────────────────

    def start(self, session_id: str) -> SessionBuffer:
        buf = SessionBuffer(self.max_items)
        self._buffers[session_id] = buf
        self._arm(session_id)
        return buf

    def add(self, session_id: str, item_type: str, content: str) -> int:
        buf = self._buffers.get(session_id)
        if buf is None:
            raise KeyError(session_id)
        count = buf.add(item_type, content)
        self._arm(session_id)  # 每来一条就重新计时（空闲超时）
        return count

    def close(self, session_id: str) -> SessionBuffer | None:
        """取出并停掉计时器"""
        self._cancel_timer(session_id)
        return self._buffers.pop(session_id, None)

    def restore(self, session_id: str, buf: SessionBuffer) -> None:
        """发送失败时把缓冲放回去，用户修好问题还能重发"""
        self._buffers[session_id] = buf
        self._arm(session_id)

    async def shutdown(self) -> None:
        for session_id in list(self._timers):
            self._cancel_timer(session_id)
        self._buffers.clear()

    # ── 计时器 ────────────────────────────────────────────

    def _arm(self, session_id: str) -> None:
        self._cancel_timer(session_id)
        try:
            self._timers[session_id] = asyncio.create_task(self._expire_later(session_id))
        except RuntimeError as e:
            logger.warning(f"⚠️ 连续发送计时器启动失败：{e}")

    def _cancel_timer(self, session_id: str) -> None:
        task = self._timers.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _expire_later(self, session_id: str) -> None:
        try:
            await asyncio.sleep(self.timeout)
        except asyncio.CancelledError:
            return
        self._timers.pop(session_id, None)
        buf = self._buffers.pop(session_id, None)
        if buf is None:
            return
        logger.info(f"⏰ 连续发送超时（{session_id}），丢弃 {buf.count} 条")
        if self.on_timeout:
            try:
                await self.on_timeout(session_id, buf)
            except Exception as e:
                logger.warning(f"⚠️ 超时回调失败：{type(e).__name__}: {e}")
