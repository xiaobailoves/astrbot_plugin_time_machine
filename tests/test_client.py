"""客户端单测：哈希算法、错误码、双模式协商

全程不联网 —— 把 _post 换成桩，只验证协议拼装与协商逻辑。
"""

import pytest

from src.client import (
    MODE_AUTO,
    MODE_CRX,
    MODE_WEIXIN,
    WEIXIN_SALT_PREFIX,
    WEIXIN_SALT_SUFFIX,
    TimeMachineClient,
    TimeMachineError,
    describe_code,
    is_auth_failure,
    make_time_code,
    parse_upload_response,
    time_code_crx,
    time_code_weixin,
)


class StubClient(TimeMachineClient):
    """按脚本回话的桩客户端"""

    def __init__(self, replies, **kwargs):
        kwargs.setdefault("blog_url", "https://blog.example.com")
        kwargs.setdefault("timecode", "SECRET")
        kwargs.setdefault("cid", 3)
        super().__init__(**kwargs)
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def _post(self, params: dict) -> str:
        self.calls.append(params)
        if not self.replies:
            raise AssertionError("桩的响应已经用完，但还在继续发请求")
        return self.replies.pop(0)


def make(**kwargs) -> StubClient:
    kwargs.setdefault("replies", ["1"])
    return StubClient(**kwargs)


# ── time_code 算法 ─────────────────────────────────────────


def test_crx_mode_is_plain_md5():
    """token=crx 是裸 md5，与 Chrome 插件 $.md5(key) 一致"""
    assert time_code_crx("") == "d41d8cd98f00b204e9800998ecf8427e"
    assert time_code_crx("abc") == "900150983cd24fb0d6963f7d28e17f72"


@pytest.mark.parametrize(
    "code,expected",
    [
        # 这几组是用 node 跑参考实现 time_machine/js/jQuery.md5.js 现算出来的，
        # 用来锁死「中文编码」「空格/井号」这类容易和 JS 走岔的输入
        ("SECRET", "44c7be48226ebad5dca8216674cad62b"),
        ("时光机编码", "dce5c8ecdfaabd186033c3f053e00e5e"),
        ("a b c #私密", "894768def332cbf6f21e145d4c2d75f9"),
        ("0" * 64, "10eab6008d5642cf42abd2aa41f847cb"),
    ],
)
def test_crx_matches_reference_js_implementation(code, expected):
    assert time_code_crx(code) == expected


def test_weixin_salts_match_reference_implementation():
    """盐必须与 wechat_for_handsome/cross.php:42 逐字一致，否则校验不过"""
    assert WEIXIN_SALT_PREFIX == "handsome!@#$%^&*()-=+@#$%$"
    assert WEIXIN_SALT_SUFFIX == "handsome!@#$%^&*()-=+@#$%$@#$%^&*"


def test_weixin_mode_wraps_code_with_salts():
    import hashlib

    expected = hashlib.md5(
        f"{WEIXIN_SALT_PREFIX}abc{WEIXIN_SALT_SUFFIX}".encode("utf-8")
    ).hexdigest()
    assert time_code_weixin("abc") == expected
    assert time_code_weixin("abc") != time_code_crx("abc")


def test_make_time_code_dispatches_by_mode():
    assert make_time_code("abc", MODE_CRX) == time_code_crx("abc")
    assert make_time_code("abc", MODE_WEIXIN) == time_code_weixin("abc")


def test_non_ascii_code_is_utf8_encoded():
    """中日文编码也要和 JS/PHP 端一致（都用 UTF-8 字节做 md5）"""
    import hashlib

    assert time_code_crx("编码") == hashlib.md5("编码".encode("utf-8")).hexdigest()


# ── 错误码翻译 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", "发送成功"),
        (" -3 ", "身份编码错误（验证编码不对，或 token 模式不匹配）"),
        ("-1", "请求参数错误（参数名或格式和主题版本对不上）"),
        ("-2", "信息缺失（cid / content 等必填项没传到位）"),
    ],
)
def test_describe_code(raw, expected):
    assert describe_code(raw) == expected


def test_describe_code_keeps_raw_for_unknown():
    assert "1999" in describe_code("1999")
    assert "(空)" in describe_code("")


def test_is_auth_failure_handles_both_shapes():
    assert is_auth_failure("-3")
    assert is_auth_failure('{"status":"-3"}')
    assert not is_auth_failure("1")
    assert not is_auth_failure("-1")
    assert not is_auth_failure("不是 json")


def test_parse_upload_response():
    ok, url, err = parse_upload_response('{"status":"1","data":"https://b/i.jpg"}')
    assert ok and url == "https://b/i.jpg" and err == ""

    ok, url, _ = parse_upload_response('{"status":"-3"}')
    assert not ok and url == ""

    ok, _, err = parse_upload_response("连接失败页面")
    assert not ok and "不是 JSON" in err

    ok, _, err = parse_upload_response('{"status":"1"}')
    assert not ok and "没有图片地址" in err


# ── token 模式协商 ─────────────────────────────────────────


async def test_auto_tries_crx_first_then_falls_back():
    client = StubClient(["-3", "1"], token_mode=MODE_AUTO)

    assert await client.send_talk("hi") == "1"
    assert [c["token"] for c in client.calls] == [MODE_CRX, MODE_WEIXIN]
    assert client.calls[1]["time_code"] == time_code_weixin("SECRET")
    assert client.detected_mode == MODE_WEIXIN


async def test_auto_remembers_negotiated_mode():
    client = StubClient(["-3", "1"], token_mode=MODE_AUTO)
    await client.send_talk("第一条")

    client.replies = ["1"]
    await client.send_talk("第二条")

    assert len(client.calls) == 3
    assert client.calls[2]["token"] == MODE_WEIXIN  # 第二次直接用协商结果，不再试探
    assert client.calls[2]["time_code"] == time_code_weixin("SECRET")


async def test_detected_mode_is_reported_once():
    seen = []
    client = StubClient(["-3", "1"], token_mode=MODE_AUTO, on_mode_detected=seen.append)
    await client.send_talk("hi")
    assert seen == [MODE_WEIXIN]


async def test_cached_mode_is_used_directly():
    client = StubClient(["1"], token_mode=MODE_AUTO, detected_mode=MODE_WEIXIN)
    await client.send_talk("hi")
    assert [c["token"] for c in client.calls] == [MODE_WEIXIN]


async def test_locked_mode_does_not_fall_back():
    client = StubClient(["-3"], token_mode=MODE_CRX)
    with pytest.raises(TimeMachineError) as exc:
        await client.send_talk("hi")
    assert "身份编码错误" in str(exc.value)
    assert len(client.calls) == 1  # 配置锁定就只试一次


async def test_non_auth_error_does_not_retry_other_mode():
    client = StubClient(["-2"], token_mode=MODE_AUTO)
    with pytest.raises(TimeMachineError):
        await client.send_talk("hi")
    assert len(client.calls) == 1  # -2 不是模式问题，没必要换模式


async def test_send_talk_error_carries_raw_response():
    client = StubClient(["-3", "-3"], token_mode=MODE_AUTO)
    with pytest.raises(TimeMachineError) as exc:
        await client.send_talk("hi")
    assert exc.value.raw.strip() == "-3"
    assert "原始响应" in exc.value.detail()


async def test_send_talk_payload_matches_reference_implementation():
    """参数名和 Chrome 插件（oper.js:282-291）保持一一对应"""
    client = StubClient(["1"])
    await client.send_talk("你好", msg_type="text")

    params = client.calls[0]
    assert params["action"] == "send_talk"
    assert params["token"] == MODE_CRX
    assert params["cid"] == "3"
    assert params["content"] == "你好"
    assert params["msg_type"] == "text"
    assert params["mediaId"] == "1"
    assert params["time_code"] == time_code_crx("SECRET")


async def test_upload_image_negotiates_and_returns_url():
    client = StubClient(
        ['{"status":"-3"}', '{"status":"1","data":"https://blog/u/a.jpg"}'],
        token_mode=MODE_AUTO,
    )
    url = await client.upload_image("data:image/png;base64,AAAA", ".png")
    assert url == "https://blog/u/a.jpg"
    assert [c["token"] for c in client.calls] == [MODE_CRX, MODE_WEIXIN]
    assert client.calls[1]["type"] == ".png"
    assert client.calls[1]["action"] == "upload_img"


async def test_upload_image_raises_on_theme_error():
    client = StubClient(['{"status":"-1"}'], token_mode=MODE_CRX)
    with pytest.raises(TimeMachineError) as exc:
        await client.upload_image("data:image/png;base64,AAAA")
    assert "参数错误" in str(exc.value)


# ── 配置自检 ───────────────────────────────────────────────


async def test_unconfigured_client_fails_fast():
    client = TimeMachineClient(blog_url="", timecode="", cid=0)
    assert not client.configured
    assert set(client.missing_fields()) == {"博客地址", "时光机验证编码", "时光机 cid"}
    with pytest.raises(TimeMachineError) as exc:
        await client.send_talk("hi")
    assert "还没配置好" in str(exc.value)


def test_mode_label():
    assert "crx" in make(token_mode=MODE_CRX).mode_label()
    assert "自动协商结果" in make(token_mode=MODE_AUTO, detected_mode=MODE_WEIXIN).mode_label()
    assert "auto" in make(token_mode=MODE_AUTO).mode_label()


def test_unknown_mode_config_falls_back_to_auto():
    client = make(token_mode="乱写的")
    assert client.mode_config == MODE_AUTO
