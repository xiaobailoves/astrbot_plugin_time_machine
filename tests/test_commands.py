"""命令处理器测试 —— 用假 event 驱动真正的异步生成器

这些是用户实际会走到的路径：发说说、连续发送、状态查询、探测。
"""

import json

from test_main_stub import FakeClient, FakeComp, jpeg_bytes, main, make_plugin
from test_main_stub import _Image as StubImage
from test_main_stub import _Reply as StubReply

SESSION = "aiocqhttp:group:10086"


class FakeEvent:
    """够用的假事件：命令处理器只用到这几个成员"""

    def __init__(self, message_str="", session=SESSION, images=None, admin=True):
        self.message_str = message_str
        self.unified_msg_origin = session
        self._images = list(images or [])
        self._admin = admin
        self.stopped = False

    def plain_result(self, text):
        return ("plain", text)

    def get_messages(self):
        return list(self._images)

    def is_admin(self):
        return self._admin

    def stop_event(self):
        self.stopped = True


async def run(agen) -> list:
    return [item async for item in agen]


def texts(out: list) -> str:
    return "\n".join(payload for _, payload in out)


# ── post ───────────────────────────────────────────────────


async def test_post_command_sends_text():
    plugin = make_plugin()
    plugin.client = FakeClient()

    out = await run(plugin.post_command(FakeEvent("/time_machine post 今天天气不错")))

    assert plugin.client.talks == [("今天天气不错", "text")]
    assert "已发到时光机" in texts(out)


async def test_post_command_marks_secret_with_hash_prefix():
    plugin = make_plugin()
    plugin.client = FakeClient()

    await run(plugin.post_command(FakeEvent("/tm 说说 #悄悄话")))

    assert plugin.client.talks[0][0] == "[secret]悄悄话[/secret]"


async def test_post_command_keeps_spaces_in_content():
    plugin = make_plugin()
    plugin.client = FakeClient()

    await run(plugin.post_command(FakeEvent("/time_machine post   多  个   空格  ")))

    assert plugin.client.talks[0][0] == "多  个   空格"


async def test_post_command_with_image_only():
    plugin = make_plugin()
    plugin.client = FakeClient()
    event = FakeEvent(
        "/time_machine post",
        images=[FakeComp(file="http://cdn.example.com/a.jpg", payload=jpeg_bytes())],
    )

    await run(plugin.post_command(event))

    content, msg_type = plugin.client.talks[0]
    assert msg_type == "text"
    assert content == "<img src='https://blog.example.com/u/1.jpg' />"


async def test_post_command_takes_images_from_the_replied_message():
    """回复一条带图消息再 post，应该把被回复的图也带上"""
    plugin = make_plugin()
    plugin.client = FakeClient()
    replied = StubReply(chain=[StubImage(file="http://cdn.example.com/a.jpg")])
    event = FakeEvent("/time_machine post 我的评论", images=[replied])

    await run(plugin.post_command(event))

    content, _ = plugin.client.talks[0]
    assert content.startswith("我的评论")
    assert content.count("<img src=") == 1


async def test_post_command_ignores_reply_without_chain():
    """适配器没填充被引用消息时就当没有图，不能报错"""
    plugin = make_plugin()
    plugin.client = FakeClient()
    event = FakeEvent("/time_machine post 只有文字", images=[StubReply()])

    await run(plugin.post_command(event))

    assert plugin.client.talks[0][0] == "只有文字"


async def test_post_command_without_anything_warns():
    plugin = make_plugin()
    plugin.client = FakeClient()

    out = await run(plugin.post_command(FakeEvent("/time_machine post")))

    assert "没有内容可发" in texts(out)
    assert plugin.client.talks == []


async def test_post_command_reports_failure():
    plugin = make_plugin()
    plugin.client = FakeClient(fail_with="身份编码错误（验证编码不对）")

    out = await run(plugin.post_command(FakeEvent("/time_machine post 会失败")))

    assert "❌" in texts(out)
    assert "身份编码错误" in texts(out)
    assert plugin.store.get("last_result").startswith("发送失败")


async def test_post_command_never_leaks_the_code():
    plugin = make_plugin(timecode="TOPSECRET")
    plugin.client = FakeClient(fail_with="服务器回显 TOPSECRET 这个值")

    out = await run(plugin.post_command(FakeEvent("/time_machine post x")))

    assert "TOPSECRET" not in texts(out)
    assert "***" in texts(out)


async def test_post_command_appends_when_buffering():
    plugin = make_plugin()
    plugin.client = FakeClient()
    plugin.buffers.start(SESSION)

    out = await run(plugin.post_command(FakeEvent("/time_machine post 素材一")))

    assert "已记录" in texts(out)
    assert plugin.client.talks == []  # 攒着，还没发
    assert plugin.buffers.get(SESSION).count == 1
    await plugin.buffers.shutdown()


# ── start / end / cancel ───────────────────────────────────


async def test_start_command_reports_previous_content():
    plugin = make_plugin()
    plugin.buffers.start(SESSION)
    plugin.buffers.add(SESSION, "text", "上一轮的残留")

    out = await run(plugin.start_command(FakeEvent("/time_machine start")))

    assert "已丢弃上一轮的 1 条" in texts(out)
    assert plugin.buffers.get(SESSION).count == 0  # 新的一轮是空的
    await plugin.buffers.shutdown()


async def test_end_command_sends_one_mixed_talk():
    plugin = make_plugin()
    plugin.client = FakeClient()
    plugin.buffers.start(SESSION)
    await plugin._buffer_append(SESSION, "第一段", [FakeComp(payload=jpeg_bytes())])

    out = await run(plugin.end_command(FakeEvent("/time_machine end")))

    content, msg_type = plugin.client.talks[0]
    assert msg_type == "mixed_talk"
    assert json.loads(content)["results"] == [
        {"type": "text", "content": "第一段"},
        {"type": "image", "content": "https://blog.example.com/u/1.jpg"},
    ]
    assert "合并成一条说说" in texts(out)
    assert not plugin.buffers.is_active(SESSION)
    await plugin.buffers.shutdown()


async def test_end_command_keeps_content_when_send_fails():
    plugin = make_plugin()
    plugin.client = FakeClient(fail_with="请求超时")
    plugin.buffers.start(SESSION)
    plugin.buffers.add(SESSION, "text", "别丢了我")

    out = await run(plugin.end_command(FakeEvent("/time_machine end")))

    assert "已保留" in texts(out)
    assert plugin.buffers.is_active(SESSION)  # 放回去了，可以重试
    assert plugin.buffers.get(SESSION).count == 1
    await plugin.buffers.shutdown()


async def test_end_command_without_buffer():
    plugin = make_plugin()
    plugin.client = FakeClient()

    out = await run(plugin.end_command(FakeEvent("/time_machine end")))

    assert "没有在攒内容" in texts(out)
    assert plugin.client.talks == []


async def test_cancel_command_drops_content():
    plugin = make_plugin()
    plugin.buffers.start(SESSION)
    plugin.buffers.add(SESSION, "text", "不要了")

    out = await run(plugin.cancel_command(FakeEvent("/time_machine cancel")))

    assert "丢弃了 1 条" in texts(out)
    assert not plugin.buffers.is_active(SESSION)


async def test_cancel_command_without_buffer():
    plugin = make_plugin()
    out = await run(plugin.cancel_command(FakeEvent("/time_machine cancel")))
    assert "没有在攒内容" in texts(out)


# ── status / test / help ───────────────────────────────────


async def test_status_command_hides_code_and_shows_negotiated_mode():
    plugin = make_plugin(timecode="MY-SECRET-CODE")
    plugin.client = FakeClient()
    plugin.client.mode_config = "auto"
    plugin.client.detected_mode = "weixin"

    body = texts(await run(plugin.status_command(FakeEvent("/time_machine status"))))

    assert "MY-SECRET-CODE" not in body
    assert "MY****DE" in body
    assert "博客地址：https://blog.example.com" in body
    assert "时光机 cid：7" in body


async def test_test_command_reports_all_four_combinations():
    plugin = make_plugin()
    plugin.client = FakeClient()

    out = await run(plugin.test_command(FakeEvent("/time_machine test")))
    body = texts(out)

    assert "✅ token=crx + 裸 md5" in body
    assert "❌ token=crx + 加盐 md5" in body
    assert "❌ token=weixin + 裸 md5" in body
    assert "❌ token=weixin + 加盐 md5" in body
    assert "本插件正常工作在这个组合上" in body
    assert "走不通" in body  # 微信那条路要给出结论
    assert len(out) == 2  # 先回一句「正在探测」，再回结果


# ── 探测结论翻译 ───────────────────────────────────────────


def test_probe_conclusion_when_nothing_passes_mentions_the_default_trap():
    text = main._probe_conclusion([])
    assert "default" in text
    assert "时光机身份验证编码" in text


def test_probe_conclusion_notes_wechat_works_when_its_combo_passes():
    text = main._probe_conclusion([("weixin", "salted")])
    assert "可以直接用" in text
    assert "本插件正常工作" in text


def test_probe_conclusion_flags_unsupported_combination():
    """weixin + 裸 md5 能过，但插件没有这种模式，要如实说明"""
    text = main._probe_conclusion([("weixin", "plain")])
    assert "还不支持" in text
    assert "走不通" in text


def test_probe_conclusion_names_the_fix_for_wechat_server():
    """只有 crx 能过时，说明 token 有意义，微信那边要连 token 一起改"""
    text = main._probe_conclusion([("crx", "plain")])
    assert "cross.php" in text
    assert "token 改成 crx" in text
    assert "裸 md5" in text


def test_probe_conclusion_says_token_does_not_matter_when_both_pass():
    """两种 token 配同一种哈希都能过 → 主题只看哈希，别让人白改 token"""
    text = main._probe_conclusion([("crx", "plain"), ("weixin", "plain")])
    assert "主题不看 token" in text
    assert "token 不用动" in text
    assert "token 改成" not in text


async def test_test_command_refuses_unconfigured():
    plugin = make_plugin()
    plugin.client = FakeClient()
    plugin.client.configured = False
    plugin.client.missing_fields = lambda: ["博客地址"]

    body = texts(await run(plugin.test_command(FakeEvent("/time_machine test"))))

    assert "还没配置好" in body
    assert "博客地址" in body


async def test_help_command():
    plugin = make_plugin()
    body = texts(await run(plugin.help_command(FakeEvent("/time_machine help"))))
    assert "/time_machine post" in body


# ── 连续发送收集器 ─────────────────────────────────────────


async def test_collector_swallows_plain_messages_while_buffering():
    plugin = make_plugin()
    plugin.client = FakeClient()
    plugin.buffers.start(SESSION)
    event = FakeEvent("随手发的一条素材")

    out = await run(plugin.buffer_collector(event))

    assert event.stopped is True  # 挡住了 LLM
    assert "已记录 1 条" in texts(out)
    await plugin.buffers.shutdown()


async def test_collector_does_nothing_when_idle():
    plugin = make_plugin()
    event = FakeEvent("普通聊天")

    out = await run(plugin.buffer_collector(event))

    assert out == []
    assert event.stopped is False


async def test_collector_ignores_commands():
    plugin = make_plugin()
    plugin.buffers.start(SESSION)
    event = FakeEvent("/time_machine end")

    out = await run(plugin.buffer_collector(event))

    assert out == []
    assert event.stopped is False  # 交给命令处理器
    assert plugin.buffers.is_active(SESSION)
    await plugin.buffers.shutdown()


async def test_collector_accepts_image_only_messages():
    """纯图片消息的 message_str 是空的——之前会被当成命令静默丢掉"""
    plugin = make_plugin()
    plugin.client = FakeClient()
    plugin.buffers.start(SESSION)
    event = FakeEvent(
        "", images=[FakeComp(file="http://cdn.example.com/a.jpg", payload=jpeg_bytes())]
    )

    out = await run(plugin.buffer_collector(event))

    buf = plugin.buffers.get(SESSION)
    assert buf.count == 1
    assert buf.image_count == 1
    assert "已记录 1 条" in texts(out)
    await plugin.buffers.shutdown()


async def test_collector_speaks_up_when_nothing_can_be_collected():
    """收不到内容时必须出声，不能像以前那样静默消失"""
    plugin = make_plugin()
    plugin.buffers.start(SESSION)

    out = await run(plugin.buffer_collector(FakeEvent("")))

    assert "没找到可发送的内容" in texts(out)
    assert "（空消息链）" in texts(out)
    await plugin.buffers.shutdown()


async def test_collector_reports_component_types_when_it_cannot_use_them():
    """有组件但取不出图时，把组件类型报出来，好定位是适配器没带图还是插件没认出来"""
    plugin = make_plugin()
    plugin.buffers.start(SESSION)

    out = await run(plugin.buffer_collector(FakeEvent("", images=[StubReply()])))

    assert "_Reply" in texts(out)
    await plugin.buffers.shutdown()


async def test_collector_ignores_non_admin_messages():
    plugin = make_plugin()
    plugin.buffers.start(SESSION)
    event = FakeEvent("路人甲插话", admin=False)

    out = await run(plugin.buffer_collector(event))

    assert out == []
    assert event.stopped is False
    assert plugin.buffers.get(SESSION).count == 0
    await plugin.buffers.shutdown()
