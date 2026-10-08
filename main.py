"""AstrBot 时光机发布插件

在任何 AstrBot 接入的平台里，直接跟机器人说话就能发 Handsome 主题的时光机说说。
原理和 Chrome 插件 / 微信公众号版一样：本插件只是瘦客户端，真正写库的是主题内置的
send_talk / upload_img 接口。
"""

import asyncio
import base64
import logging
import re
import time
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp

from .src.buffer import BufferFull, BufferStore, ITEM_IMAGE, ITEM_TEXT
from .src.client import (
    HASH_PLAIN,
    HASH_SALTED,
    MODE_AUTO,
    MODE_HASH,
    MODE_WEIXIN,
    MODES,
    TimeMachineClient,
    TimeMachineError,
)
from .src.content import apply_secret, image_tag
from .src.media import MediaError, to_data_url
from .src.store import StateStore

try:  # GreedyStr 能把命令后面的剩余全文当一个参数收下
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # 老版本 AstrBot 没有这个类型，正文靠下面的正则兜底
    GreedyStr = str


PLUGIN_NAME = "astrbot_plugin_time_machine"

# 命令名与别名。改这里就能改命令，正则和装饰器共用同一份。
GROUP_NAMES = ("time_machine", "tm")
POST_NAMES = ("post", "说说", "talk", "send")
START_NAMES = ("start", "开始")
END_NAMES = ("end", "结束")
CANCEL_NAMES = ("cancel", "取消")
STATUS_NAMES = ("status", "状态")
TEST_NAMES = ("test", "测试")
HELP_NAMES = ("help", "帮助")

# 一条说说最多带几张图，防止一次上传太多把博客拖垮
MAX_IMAGES_PER_POST = 9

HASH_LABEL = {HASH_PLAIN: "裸 md5", HASH_SALTED: "加盐 md5"}

HELP_TEXT = """🕰️ 时光机插件

/time_machine post <内容>    发一条说说，# 开头是私密说说
/time_machine post           配图发送，或回复一条带图消息发送
/time_machine start          开启连续发送（之后直接发消息即可）
/time_machine end            结束并把这轮内容合并成一条图文说说
/time_machine cancel         放弃这轮内容
/time_machine status         查看配置和最近一次发送结果
/time_machine test           探测身份校验（会上传一张 1×1 空白图，不产生说说）
/time_machine help           这条帮助

支持的命令别名：/tm、说说、开始/结束/取消、状态、测试、帮助"""


def _reply_type() -> type | tuple:
    """拿 Comp.Reply 的类型。老版本没有这个组件时返回空元组，isinstance 永远为假。"""
    return getattr(Comp, "Reply", ())


def _probe_conclusion(working: list[tuple[str, str]]) -> str:
    """把探测结果翻译成人话，并指出微信服务端那条路能不能走"""
    if not working:
        return (
            "结论：四种组合全都没过。先确认主题设置里的「时光机身份验证编码」"
            "不是空的、也不是默认值 default——主题源码里这两个值会让校验直接失败。"
        )

    combos = "；".join(f"token={m} + {HASH_LABEL[h]}" for m, h in working)
    text = f"结论：主题接受 {combos}。"

    # 两种 token 配同一种哈希都能过 → 主题根本不看 token，只看哈希
    token_irrelevant = len({m for m, _ in working}) > 1 and len({h for _, h in working}) == 1

    if any(MODE_HASH.get(m) == h for m, h in working):
        text += "本插件正常工作在这个组合上。"
    else:
        text += "本插件的发送路径还不支持这些组合，跟我说一声我来适配。"

    if (MODE_WEIXIN, HASH_SALTED) in working:
        text += " 微信服务端（wechat_for_handsome）发的正是这组，可以直接用。"
        return text

    best_mode, best_hash = working[0]
    hash_hint = f"time_code 换成{HASH_LABEL[best_hash]} 算出的值"

    text += " 微信服务端（wechat_for_handsome）发的是「token=weixin + 加盐 md5」，"
    if token_irrelevant:
        text += (
            "哈希对不上，所以那条路现在走不通；"
            f"不过它用的 token 本身没问题（主题不看 token），所以只要把 cross.php 里的 {hash_hint}就行，token 不用动。"
        )
    else:
        # 只有当微信服务端现在用的 token 和可用组合不一致时，才需要连着 token 一起改
        token_part = f"token 改成 {best_mode}，" if best_mode != MODE_WEIXIN else ""
        text += (
            "不在上面这些组合里，所以那条路现在走不通；"
            f"想复活它，得改它的 cross.php：{token_part}{hash_hint}。"
        )
    return text


def _looks_like_command(text: str) -> bool:
    """判断一条消息是不是命令，连续发送时不该把它收进缓冲"""
    if not text:
        return True
    if text[0] in "/!！":
        return True
    return text.split(maxsplit=1)[0] in GROUP_NAMES


def _strip_command(text: str, sub_names: tuple[str, ...]) -> str | None:
    """从消息里剥掉命令前缀，返回剩余全文（保留空格）；没匹配上返回 None。

    GreedyStr 能正常绑定时用不到这里，但它兼容老版本 AstrBot，
    也兼容「正文里带空格」这种被参数解析切碎的情况。
    """
    group = "|".join(re.escape(n) for n in GROUP_NAMES)
    subs = "|".join(re.escape(n) for n in sub_names)
    match = re.search(rf"(?:{group})\s+(?:{subs})\s*", text, flags=re.IGNORECASE)
    return text[match.end():] if match else None


def _sub_aliases(names: tuple[str, ...]) -> set[str]:
    return set(names[1:])


@register(
    PLUGIN_NAME,
    "Xiaobailoves",
    "时光机发布插件",
    "1.0.0",
    "https://github.com/xiaobailoves/astrbot_plugin_time_machine",
)
class TimeMachinePlugin(Star):
    # 类变量，防止热重载时留下没关掉的 connection pool（幽灵会话）
    _shared_client: TimeMachineClient | None = None

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.logger = logging.getLogger("astrbot")
        self.context = context
        self.config = config

        # ── 提取配置 ──────────────────────────────────────
        self.blog_url = (config.get("blog_url") or "").strip()
        self.timecode = (config.get("timecode") or "").strip()
        self.cid = config.get("cid") or 0
        self.token_mode = config.get("token_mode") or MODE_AUTO
        self.upload_mode = config.get("upload_mode") or "base64"
        self.image_max_edge = config.get("image_max_edge", 2560)
        self.max_image_mb = config.get("max_image_mb", 5)
        self.buffer_timeout = config.get("buffer_timeout", 300)
        self.buffer_max_items = config.get("buffer_max_items", 50)
        self.proxy = config.get("proxy", "")
        self.verify_ssl = config.get("verify_ssl", True)

        # ── 配置校验 ──────────────────────────────────────
        if self.token_mode not in MODES and self.token_mode != MODE_AUTO:
            self.logger.warning(f"⚠️ token_mode 取值 {self.token_mode} 不认识，已按 auto 处理")
            self.token_mode = MODE_AUTO
        if self.upload_mode not in ("base64", "url"):
            self.logger.warning(f"⚠️ upload_mode 取值 {self.upload_mode} 不认识，已按 base64 处理")
            self.upload_mode = "base64"
        self.max_image_mb = max(1, min(8, int(self.max_image_mb or 5)))
        self.image_max_edge = max(320, int(self.image_max_edge or 2560))
        self.buffer_timeout = max(30, int(self.buffer_timeout or 300))
        self.buffer_max_items = max(2, int(self.buffer_max_items or 50))

        # ── 状态文件 ──────────────────────────────────────
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as e:
            self.logger.warning(f"⚠️ 取插件数据目录失败，退回插件目录：{e}")
            data_dir = Path(__file__).parent / "data"
        self.store = StateStore(
            Path(data_dir) / "state.json",
            defaults={"token_mode": "", "last_result": "", "last_time": ""},
        )

        # ── 客户端（热重载时先关掉上一份）──────────────────
        self._close_stale_client(TimeMachinePlugin._shared_client)
        self.client = TimeMachineClient(
            blog_url=self.blog_url,
            timecode=self.timecode,
            cid=self.cid,
            token_mode=self.token_mode,
            proxy=self.proxy,
            verify_ssl=self.verify_ssl,
            detected_mode=self.store.get("token_mode", ""),
            on_mode_detected=self._save_detected_mode,
        )
        TimeMachinePlugin._shared_client = self.client

        # ── 连续发送缓冲 ──────────────────────────────────
        self.buffers = BufferStore(
            timeout=self.buffer_timeout,
            max_items=self.buffer_max_items,
            on_timeout=self._on_buffer_timeout,
        )

        if self.client.configured:
            self.logger.info(
                f"🕰️ 时光机插件已加载（{self.blog_url}，token 模式 {self.client.mode_label()}）"
            )
        else:
            self.logger.warning(
                f"⚠️ 时光机插件还没配置好，缺少：{'、'.join(self.client.missing_fields())}"
            )

    # ═══════════════════════════════════════════════════════════
    #  命令
    # ═══════════════════════════════════════════════════════════

    @filter.command_group("time_machine", alias={"tm"})
    def time_machine(self):
        """时光机：把消息发到博客的时光机"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @time_machine.command("post", alias=_sub_aliases(POST_NAMES))
    async def post_command(self, event: AstrMessageEvent, content: GreedyStr | None = None):
        """发一条说说。可以配图发送，也可以回复一条带图消息。"""
        session = event.unified_msg_origin
        stripped = _strip_command(event.message_str, POST_NAMES)
        text = (stripped if stripped is not None else (content or "")).strip()
        images = self._collect_images(event)

        if not text and not images:
            yield event.plain_result(
                "⚠️ 没有内容可发。\n"
                "用法：/time_machine post 想说的话\n"
                "也可以配一张图一起发，或回复一条带图消息再发本命令。"
            )
            return

        # 连续发送模式下，post 就是往缓冲里追一条
        if self.buffers.is_active(session):
            yield event.plain_result(await self._buffer_append(session, text, images))
            return

        try:
            body, notes = await self._compose(text, images)
        except (MediaError, TimeMachineError) as e:
            yield event.plain_result(self._fail(e))
            return

        try:
            await self.client.send_talk(body, msg_type="text")
        except TimeMachineError as e:
            self._record("发送失败", str(e))
            yield event.plain_result(self._fail(e))
            return

        self._record("成功", f"{len(images)} 图")
        summary = "、".join(s for s in ["文字" if text else "", f"{len(images)} 张图" if images else ""] if s)
        tip = ("\n" + "；".join(notes)) if notes else ""
        yield event.plain_result(f"🎉 已发到时光机（{summary or '空内容'}）{tip}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @time_machine.command("start", alias=_sub_aliases(START_NAMES))
    async def start_command(self, event: AstrMessageEvent):
        """开启连续发送模式"""
        session = event.unified_msg_origin
        old = self.buffers.get(session)
        self.buffers.start(session)
        head = f"🗑️ 已丢弃上一轮的 {old.count} 条内容。\n" if old and not old.empty else ""
        yield event.plain_result(
            f"{head}📝 已开启连续发送：接下来直接发文字/图片即可（最多 {self.buffer_max_items} 条，"
            f"空闲 {self.buffer_timeout}s 自动放弃）。\n"
            "发 /time_machine end 合并发送，/time_machine cancel 取消。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @time_machine.command("end", alias=_sub_aliases(END_NAMES))
    async def end_command(self, event: AstrMessageEvent):
        """结束连续发送，把攒的内容合并成一条说说发出去"""
        session = event.unified_msg_origin
        buf = self.buffers.close(session)
        if buf is None or buf.empty:
            yield event.plain_result("⚠️ 现在没有在攒内容。先用 /time_machine start 开启。")
            return

        try:
            await self.client.send_talk(buf.build(), msg_type="mixed_talk")
        except TimeMachineError as e:
            # 内容还留着，用户修好问题可以直接再 end 一次重试
            self.buffers.restore(session, buf)
            self._record("发送失败", str(e))
            yield event.plain_result(
                f"{self._fail(e)}\n（{buf.count} 条内容已保留，问题解决后再发一次 end 即可重试）"
            )
            return

        self._record("成功", f"混排 {buf.count} 条")
        yield event.plain_result(
            f"🎉 已发到时光机：{buf.count} 条合并成一条说说"
            f"（{buf.text_count} 段文字 / {buf.image_count} 张图）。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @time_machine.command("cancel", alias=_sub_aliases(CANCEL_NAMES))
    async def cancel_command(self, event: AstrMessageEvent):
        """放弃连续发送攒下的内容"""
        buf = self.buffers.close(event.unified_msg_origin)
        if buf is None or buf.empty:
            yield event.plain_result("⚠️ 现在没有在攒内容。")
            return
        yield event.plain_result(f"🗑️ 已取消，丢弃了 {buf.count} 条内容。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @time_machine.command("status", alias=_sub_aliases(STATUS_NAMES))
    async def status_command(self, event: AstrMessageEvent):
        """查看当前配置与最近一次发送结果"""
        buf = self.buffers.get(event.unified_msg_origin)
        lines = [
            "🕰️ 时光机插件状态",
            "━━ 配置 ━━",
            f"博客地址：{self.blog_url or '（未填写）'}",
            f"时光机 cid：{self.cid or '（未填写）'}",
            f"验证编码：{self._mask_code()}",
            f"token 模式：{self.client.mode_label()}",
            f"图片上传：{self.upload_mode} 模式 · 长边 ≤{self.image_max_edge}px · 单张 ≤{self.max_image_mb}MB",
            f"连续发送：{'进行中，已攒 ' + str(buf.count) + ' 条' if buf else '未开启'}"
            f"（超时 {self.buffer_timeout}s）",
            "━━ 最近一次 ━━",
            f"{self.store.get('last_time') or '还没发过'} · {self.store.get('last_result') or '-'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @time_machine.command("test", alias=_sub_aliases(TEST_NAMES))
    async def test_command(self, event: AstrMessageEvent):
        """探测主题认哪种 token / 哈希组合（不产生说说）"""
        if not self.client.configured:
            yield event.plain_result(
                "⚠️ 还没配置好，缺少：" + "、".join(self.client.missing_fields())
            )
            return

        yield event.plain_result(
            "🔍 正在探测四种组合，会上传 4 张 1×1 的空白图（不会产生任何说说）……"
        )
        results = await self.client.probe()

        lines = ["🧪 探测结果（token × 哈希）", f"地址：{self.blog_url}"]
        for mode, hash_kind, ok, detail in results:
            lines.append(
                f"{'✅' if ok else '❌'} token={mode} + {HASH_LABEL[hash_kind]}：{detail}"
            )
        lines.append("")
        lines.append(_probe_conclusion([(m, h) for m, h, ok, _ in results if ok]))
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @time_machine.command("help", alias=_sub_aliases(HELP_NAMES))
    async def help_command(self, event: AstrMessageEvent):
        """查看用法"""
        yield event.plain_result(HELP_TEXT)

    # ═══════════════════════════════════════════════════════════
    #  连续发送：顺手收下普通消息
    # ═══════════════════════════════════════════════════════════

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def buffer_collector(self, event: AstrMessageEvent):
        """连续发送进行中时，把普通消息收进缓冲，并且不让 LLM 插嘴"""
        session = event.unified_msg_origin
        if not self.buffers.is_active(session):
            return
        if not event.is_admin():
            return  # 非管理员的消息不拦，交给机器人正常处理
        text = (event.message_str or "").strip()
        if _looks_like_command(text):
            return  # 命令交给命令处理器

        images = self._collect_images(event)
        if not text and not images:
            return

        event.stop_event()
        yield event.plain_result(await self._buffer_append(session, text, images))

    # ═══════════════════════════════════════════════════════════
    #  内部逻辑
    # ═══════════════════════════════════════════════════════════

    def _collect_images(self, event: AstrMessageEvent) -> list:
        """挑出本条消息（以及被回复的那条消息）里的图片组件"""
        found: list = []

        def take(chain) -> None:
            for comp in chain or []:
                if isinstance(comp, Comp.Image) and self._image_src(comp):
                    found.append(comp)

        for comp in event.get_messages():
            if isinstance(comp, Comp.Image):
                if self._image_src(comp):
                    found.append(comp)
            elif isinstance(comp, _reply_type()):
                # 并非所有适配器都会填充被引用消息的 chain，取不到就作罢
                take(getattr(comp, "chain", None))
        return found

    @staticmethod
    def _image_src(comp) -> str:
        return getattr(comp, "url", None) or getattr(comp, "file", None) or ""

    async def _compose(self, text: str, images: list) -> tuple[str, list[str]]:
        """把文字和图片拼成可以直接发送的 content，返回 (content, 提示信息)"""
        body = apply_secret(text) if text else ""
        notes: list[str] = []
        for comp in images[:MAX_IMAGES_PER_POST]:
            url, fell_back = await self._upload_one(comp)
            if fell_back:
                notes.append("有图片改用直链交给主题下载")
            body += image_tag(url)
        if len(images) > MAX_IMAGES_PER_POST:
            notes.append(f"只发了前 {MAX_IMAGES_PER_POST} 张图")
        return body, notes

    async def _upload_one(self, comp) -> tuple[str, bool]:
        """上传一张图，返回 (图片URL, 是否降级成了直链)"""
        src = self._image_src(comp)
        is_http = src.startswith(("http://", "https://"))

        if self.upload_mode == "url" and is_http:
            return await self.client.upload_image(src, ".jpg"), True

        data = ""
        try:
            # 框架自带：URL / 本地文件 / file:// / base64:// 都能吃
            raw_b64 = (await comp.convert_to_base64()).strip()
            # 正常返回的是裸 base64，这里顺手容忍各种前缀写法
            if raw_b64.startswith("base64://"):
                raw_b64 = raw_b64[len("base64://"):]
            elif "," in raw_b64[:64]:
                raw_b64 = raw_b64.split(",", 1)[1]
            data = to_data_url(
                base64.b64decode(raw_b64),
                max_edge=self.image_max_edge,
                max_bytes=self.max_image_bytes(),
            )
        except Exception as e:
            self.logger.warning(f"⚠️ 取图/压图失败（{type(e).__name__}: {e}）")
            if not is_http:
                raise MediaError(f"这张图片取不到数据：{type(e).__name__}: {e}") from e
            # 手里有直链，就交给主题去下载
            return await self.client.upload_image(src, ".jpg"), True

        return await self.client.upload_image(data, ".jpg"), False

    async def _buffer_append(self, session: str, text: str, images: list) -> str:
        buf = self.buffers.get(session)
        if buf is None:
            return "⚠️ 连续发送已经结束了，先 /time_machine start 重新开启。"

        added = 0
        try:
            if text:
                self.buffers.add(session, ITEM_TEXT, apply_secret(text))
                added += 1
            for comp in images:
                url, _ = await self._upload_one(comp)
                self.buffers.add(session, ITEM_IMAGE, url)
                added += 1
        except BufferFull as e:
            return f"⚠️ {e}"
        except (MediaError, TimeMachineError) as e:
            return f"{self._fail(e)}\n（已收下的 {added} 条还在，可以继续补或直接 end）"

        buf = self.buffers.get(session)
        if buf is None:
            return "⚠️ 连续发送已经结束了。"
        return (
            f"📥 已记录 {added} 条，目前共 {buf.count} 条"
            f"（{buf.text_count} 段文字 / {buf.image_count} 张图）。\n"
            f"最新：{buf.preview()}"
        )

    async def _on_buffer_timeout(self, session: str, buf) -> None:
        """连续发送闲太久，通知一下并把内容丢掉"""
        try:
            await self.context.send_message(
                session,
                MessageChain(
                    chain=[
                        Comp.Plain(
                            f"⏰ 连续发送空闲超过 {self.buffer_timeout}s，这轮攒的 {buf.count} 条内容已丢弃。"
                        )
                    ]
                ),
            )
        except Exception as e:
            self.logger.warning(f"⚠️ 连续发送超时通知发送失败：{type(e).__name__}: {e}")

    # ── 小工具 ────────────────────────────────────────────

    def _close_stale_client(self, old: TimeMachineClient | None) -> None:
        """热重载时把上一份的会话关掉，免得留下没释放的连接池"""
        if old is None or not old.has_session:
            return
        coro = old.close()
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            # 不在事件循环里（正常加载时不会），至少别留下没 await 的协程
            coro.close()
            self.logger.warning("⚠️ 没有运行中的事件循环，旧会话交给 GC 回收")

    def max_image_bytes(self) -> int:
        return self.max_image_mb * 1024 * 1024

    def _mask_code(self) -> str:
        code = self.timecode
        if not code:
            return "（未填写）"
        if len(code) <= 4:
            return "*" * len(code)
        return f"{code[:2]}{'*' * 4}{code[-2:]}（共 {len(code)} 位）"

    def _scrub(self, text: str) -> str:
        """万一主题把参数回显了，别让验证编码漏到聊天里"""
        if self.timecode and self.timecode in text:
            text = text.replace(self.timecode, "***")
        return text

    def _fail(self, e: Exception) -> str:
        detail = e.detail() if isinstance(e, TimeMachineError) else str(e)
        return "❌ " + self._scrub(detail)

    def _record(self, result: str, detail: str = "") -> None:
        self.store.set("last_result", f"{result} {detail}".strip())
        self.store.set("last_time", time.strftime("%Y-%m-%d %H:%M:%S"))
        self.store.save()

    def _save_detected_mode(self, mode: str) -> None:
        self.store.set("token_mode", mode)
        self.store.save()

    # ── 生命周期 ──────────────────────────────────────────

    async def terminate(self) -> None:
        try:
            await self.buffers.shutdown()
        except Exception as e:
            self.logger.warning(f"⚠️ 关闭连续发送缓冲失败：{e}")
        try:
            await self.client.close()
        except Exception as e:
            self.logger.warning(f"⚠️ 关闭时光机客户端失败：{e}")
        if TimeMachinePlugin._shared_client is self.client:
            TimeMachinePlugin._shared_client = None
        self.logger.info("🕰️ 时光机插件已卸载")
