"""用桩模块把 main.py 导进来做冒烟测试

本机没有 AstrBot，这里把 astrbot.* 用最小实现塞进 sys.modules，
至少能验证：导入路径、装饰器用法、命令函数签名、配置解析都没写错。
"""

import importlib.util
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STUB_DATA_DIR = Path(tempfile.gettempdir()) / "astrbot_plugin_time_machine_stub"


# ── 搭 astrbot 桩 ──────────────────────────────────────────


class _Commandable:
    def __init__(self, func):
        self.func = func

    def command(self, name=None, alias=None, **kwargs):
        def deco(fn):
            return fn

        return deco


class _Filter:
    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    class EventMessageType:
        ALL = "all"
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"

    @staticmethod
    def command_group(name=None, alias=None, **kwargs):
        def deco(fn):
            return _Commandable(fn)

        return deco

    @staticmethod
    def command(name=None, alias=None, **kwargs):
        return lambda fn: fn

    @staticmethod
    def permission_type(permission_type, **kwargs):
        return lambda fn: fn

    @staticmethod
    def event_message_type(event_message_type, **kwargs):
        return lambda fn: fn


class _Plain:
    def __init__(self, text="", **kwargs):
        self.text = text


class _Image:
    def __init__(self, file=None, **kwargs):
        self.file = file
        self.url = kwargs.get("url", "")


class _Reply:
    """被引用的消息，chain 里是被引用消息的组件（部分适配器不填充）"""

    def __init__(self, chain=None, **kwargs):
        self.chain = chain or []


class _Star:
    def __init__(self, context=None, config=None):
        self.context = context

    async def terminate(self):
        pass


class _StarTools:
    @staticmethod
    def get_data_dir(plugin_name=None):
        path = STUB_DATA_DIR / (plugin_name or "unknown")
        path.mkdir(parents=True, exist_ok=True)
        return path


class _AstrBotConfig(dict):
    pass


def _install_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = _AstrBotConfig

    api_event = types.ModuleType("astrbot.api.event")
    api_event.filter = _Filter
    api_event.AstrMessageEvent = type("AstrMessageEvent", (), {})

    class _MessageChain:
        def __init__(self, chain=None, **kwargs):
            self.chain = chain or []

    api_event.MessageChain = _MessageChain

    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = type("Context", (), {})
    api_star.Star = _Star
    api_star.StarTools = _StarTools

    def _register(name, author, desc, version, repo=None):
        return lambda cls: cls

    api_star.register = _register

    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = _Plain
    components.Image = _Image
    components.Reply = _Reply

    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    core_star_filter = types.ModuleType("astrbot.core.star.filter")
    core_star_filter_command = types.ModuleType("astrbot.core.star.filter.command")

    class GreedyStr(str):
        """和 AstrBot 一样：吃掉命令之后的剩余全文"""

    core_star_filter_command.GreedyStr = GreedyStr

    for name, module in [
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", api_event),
        ("astrbot.api.star", api_star),
        ("astrbot.api.message_components", components),
        ("astrbot.core", core),
        ("astrbot.core.star", core_star),
        ("astrbot.core.star.filter", core_star_filter),
        ("astrbot.core.star.filter.command", core_star_filter_command),
    ]:
        sys.modules[name] = module

    astrbot.api = api
    api.event = api_event
    api.star = api_star
    api.message_components = components


def _load_main():
    """像 AstrBot 那样把插件当包加载（main.py 里用了相对导入）"""
    _install_stubs()
    package_name = "astrbot_plugin_time_machine"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT)]
        sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(f"{package_name}.main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


main = _load_main()


@pytest.fixture(autouse=True)
def clean_stub_data_dir():
    yield
    shutil.rmtree(STUB_DATA_DIR, ignore_errors=True)


def make_plugin(**overrides):
    config = {
        "blog_url": "https://blog.example.com",
        "timecode": "MY-SECRET-CODE",
        "cid": 7,
        "token_mode": "auto",
        "upload_mode": "base64",
        "image_max_edge": 2560,
        "max_image_mb": 5,
        "buffer_timeout": 300,
        "buffer_max_items": 50,
        "proxy": "",
        "verify_ssl": True,
    }
    config.update(overrides)
    return main.TimeMachinePlugin(None, config)


# ── 冒烟 ───────────────────────────────────────────────────


def test_plugin_imports_and_registers_expected_commands():
    """命令函数都挂上去了，装饰器没有把它们吃掉"""
    for name in (
        "post_command",
        "start_command",
        "end_command",
        "cancel_command",
        "status_command",
        "test_command",
        "help_command",
        "buffer_collector",
    ):
        assert hasattr(main.TimeMachinePlugin, name), f"缺少命令处理函数 {name}"


def test_plugin_constructs_with_valid_config():
    plugin = make_plugin()
    assert plugin.client.configured
    assert plugin.cid == 7
    assert plugin.max_image_bytes() == 5 * 1024 * 1024
    assert plugin.buffers.max_items == 50


def test_plugin_survives_missing_config():
    plugin = make_plugin(blog_url="", timecode="", cid=0)
    assert not plugin.client.configured
    assert set(plugin.client.missing_fields()) == {"博客地址", "时光机验证编码", "时光机 cid"}


def test_bad_config_values_are_corrected():
    plugin = make_plugin(token_mode="瞎写的", upload_mode="瞎写的", max_image_mb=99, image_max_edge=1)
    assert plugin.token_mode == "auto"
    assert plugin.upload_mode == "base64"
    assert plugin.max_image_mb == 8  # 夹到上限
    assert plugin.image_max_edge == 320  # 夹到下限


def test_state_file_is_created_on_disk():
    plugin = make_plugin()
    plugin._record("成功", "1 图")
    state_file = Path(plugin.store.path)

    assert state_file.exists()
    assert "成功" in state_file.read_text(encoding="utf-8")
    # 状态文件里绝不该出现验证编码
    assert "MY-SECRET-CODE" not in state_file.read_text(encoding="utf-8")


# ── 命令前缀解析 ───────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/time_machine post 你好 世界", "你好 世界"),
        ("/tm 说说 测试一下", "测试一下"),
        ("time_machine send 没有斜杠", "没有斜杠"),
        ("/time_machine post", ""),
        ("/time_machine post    前后有空格   ", "前后有空格   "),
    ],
)
def test_strip_command(text, expected):
    assert main._strip_command(text, main.POST_NAMES) == expected


def test_strip_command_returns_none_when_not_a_command():
    assert main._strip_command("今天天气不错", main.POST_NAMES) is None


def test_strip_command_with_end_names():
    assert main._strip_command("/time_machine end", main.END_NAMES) == ""


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/time_machine post x", True),
        ("/任何命令", True),
        ("!bang", True),
        ("time_machine post x", True),
        ("tm start", True),
        ("今天天气不错", False),
        ("", True),
    ],
)
def test_looks_like_command(text, expected):
    assert main._looks_like_command(text) is expected


def test_mask_code_hides_the_secret():
    plugin = make_plugin(timecode="ABCDEFGH")
    masked = plugin._mask_code()
    assert "ABCDEFGH" not in masked
    assert masked.startswith("AB")


def test_scrub_removes_secret_from_error_text():
    plugin = make_plugin()
    assert "MY-SECRET-CODE" not in plugin._scrub("主题回显了 MY-SECRET-CODE 这个东西")


def test_help_text_lists_every_command():
    for name in main.POST_NAMES[:1] + main.START_NAMES[:1] + main.END_NAMES[:1]:
        assert f"/time_machine {name}" in main.HELP_TEXT
    assert "/time_machine cancel" in main.HELP_TEXT
    assert "/time_machine status" in main.HELP_TEXT


# ── 图片上传的两条分支 ─────────────────────────────────────


def jpeg_bytes() -> bytes:
    import io

    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (64, 48), (200, 100, 50)).save(buf, format="JPEG")
    return buf.getvalue()


class FakeComp(_Image):
    """冒充 astrbot 的 Comp.Image：既要是真组件类型（过 isinstance），又能控制返回内容"""

    def __init__(self, file="", url="", payload=b"", fail=False):
        super().__init__(file=file, url=url)
        self.payload = payload
        self.fail = fail

    async def convert_to_base64(self) -> str:
        if self.fail:
            raise RuntimeError("下载失败")
        import base64

        return base64.b64encode(self.payload).decode()


class FakeClient:
    def __init__(self, fail_with: str = ""):
        self.uploads: list[tuple[str, str]] = []
        self.talks: list[tuple[str, str]] = []
        self.fail_with = fail_with
        self.configured = True
        self.mode_config = "auto"
        self.detected_mode = ""

    def mode_label(self) -> str:
        return "auto（首次发送时自动协商）"

    def missing_fields(self) -> list[str]:
        return []

    async def send_talk(self, content: str, msg_type: str = "text") -> str:
        if self.fail_with:
            raise main.TimeMachineError(self.fail_with, "-3")
        self.talks.append((content, msg_type))
        return "1"

    async def upload_image(self, data: str, suffix: str = ".jpg") -> str:
        self.uploads.append((data, suffix))
        return f"https://blog.example.com/u/{len(self.uploads)}.jpg"

    async def probe(self):
        return [
            ("crx", True, "通过（探测图：https://blog.example.com/u/p.png）"),
            ("weixin", False, "身份校验失败（-3）"),
        ]


async def test_upload_one_sends_base64_when_it_can_download():
    plugin = make_plugin()
    plugin.client = FakeClient()

    url, fell_back = await plugin._upload_one(FakeComp(file="http://x/a.jpg", payload=jpeg_bytes()))

    assert url == "https://blog.example.com/u/1.jpg"
    assert fell_back is False
    assert plugin.client.uploads[0][0].startswith("data:image/jpeg;base64,")


async def test_upload_one_falls_back_to_direct_url_when_download_fails():
    plugin = make_plugin()
    plugin.client = FakeClient()

    comp = FakeComp(url="https://cdn.example.com/a.jpg", fail=True)
    url, fell_back = await plugin._upload_one(comp)

    assert fell_back is True
    assert plugin.client.uploads[0][0] == "https://cdn.example.com/a.jpg"


async def test_upload_one_fails_when_there_is_no_url_to_fall_back_to():
    plugin = make_plugin()
    plugin.client = FakeClient()

    with pytest.raises(main.MediaError):
        await plugin._upload_one(FakeComp(file="/tmp/本地图.jpg", fail=True))


async def test_upload_mode_url_skips_the_download():
    plugin = make_plugin(upload_mode="url")
    plugin.client = FakeClient()

    url, fell_back = await plugin._upload_one(
        FakeComp(url="https://cdn.example.com/a.jpg", payload=jpeg_bytes())
    )

    assert fell_back is True
    assert plugin.client.uploads[0][0] == "https://cdn.example.com/a.jpg"


# ── 内容拼装 ───────────────────────────────────────────────


async def test_compose_wraps_secret_and_appends_image_tags():
    plugin = make_plugin()
    plugin.client = FakeClient()

    body, notes = await plugin._compose("#秘密内容", [FakeComp(payload=jpeg_bytes())])

    assert body.startswith("[secret]秘密内容[/secret]")
    assert body.endswith("<img src='https://blog.example.com/u/1.jpg' />")
    assert notes == []


async def test_compose_without_text_still_produces_image_tag():
    plugin = make_plugin()
    plugin.client = FakeClient()

    body, _ = await plugin._compose("", [FakeComp(payload=jpeg_bytes())])
    assert body == "<img src='https://blog.example.com/u/1.jpg' />"


async def test_compose_caps_the_number_of_images():
    plugin = make_plugin()
    plugin.client = FakeClient()

    comps = [FakeComp(payload=jpeg_bytes()) for _ in range(main.MAX_IMAGES_PER_POST + 3)]
    body, notes = await plugin._compose("图很多", comps)

    assert body.count("<img src=") == main.MAX_IMAGES_PER_POST
    assert len(plugin.client.uploads) == main.MAX_IMAGES_PER_POST
    assert any("只发了前" in n for n in notes)


# ── 连续发送 ───────────────────────────────────────────────


async def test_buffer_append_collects_text_and_images_in_order():
    plugin = make_plugin()
    plugin.client = FakeClient()
    plugin.buffers.start("s1")

    reply = await plugin._buffer_append("s1", "第一段", [FakeComp(payload=jpeg_bytes())])

    assert "已记录 2 条" in reply
    assert "共 2 条" in reply
    buf = plugin.buffers.get("s1")
    assert (buf.text_count, buf.image_count) == (1, 1)

    import json

    assert json.loads(buf.build()) == {
        "results": [
            {"type": "text", "content": "第一段"},
            {"type": "image", "content": "https://blog.example.com/u/1.jpg"},
        ]
    }
    await plugin.buffers.shutdown()


async def test_buffer_append_applies_secret_and_keeps_earlier_items_on_failure():
    plugin = make_plugin()
    plugin.client = FakeClient()
    plugin.buffers.start("s1")

    reply = await plugin._buffer_append(
        "s1", "#悄悄话", [FakeComp(file="/tmp/x.jpg", fail=True)]
    )

    assert "已收下的 1 条还在" in reply
    buf = plugin.buffers.get("s1")
    assert buf.count == 1
    assert buf.items[0] == ("text", "[secret]悄悄话[/secret]")
    await plugin.buffers.shutdown()


async def test_buffer_append_reports_full_buffer():
    # 条数下限是 2（一轮混排至少要两条才有意义），所以这里用 2 来测
    plugin = make_plugin(buffer_max_items=2)
    plugin.client = FakeClient()
    plugin.buffers.start("s1")
    plugin.buffers.add("s1", "text", "占位一")
    plugin.buffers.add("s1", "text", "占位二")

    reply = await plugin._buffer_append("s1", "塞不下了", [])

    assert "最多攒 2 条" in reply
    assert plugin.buffers.get("s1").count == 2  # 超限的那条没被塞进去
    await plugin.buffers.shutdown()
