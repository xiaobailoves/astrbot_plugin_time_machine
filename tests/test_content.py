"""内容格式化单测：私密改写、单图标签、混排 JSON"""

import json

from src.content import SECRET_CLOSE, SECRET_OPEN, apply_secret, build_mixed, image_tag, summarize


def test_secret_prefix_is_wrapped():
    """# 开头 → [secret]...[/secret]，与两个参考实现一致"""
    assert apply_secret("#今晚月色真美") == f"{SECRET_OPEN}今晚月色真美{SECRET_CLOSE}"


def test_plain_text_untouched():
    assert apply_secret("普通说说") == "普通说说"
    assert apply_secret("") == ""


def test_lone_hash():
    assert apply_secret("#") == f"{SECRET_OPEN}{SECRET_CLOSE}"


def test_hash_not_at_start_is_kept():
    assert apply_secret("话题 #标签") == "话题 #标签"


def test_image_tag_uses_single_quotes():
    """oper.js:193 拼的是单引号，主题端按这个解析"""
    assert image_tag("https://blog/u/a.jpg") == "<img src='https://blog/u/a.jpg' />"


def test_build_mixed_preserves_order_and_types():
    items = [("text", "第一段"), ("image", "https://blog/u/1.jpg"), ("text", "第二段")]
    payload = json.loads(build_mixed(items))

    assert payload == {
        "results": [
            {"type": "text", "content": "第一段"},
            {"type": "image", "content": "https://blog/u/1.jpg"},
            {"type": "text", "content": "第二段"},
        ]
    }


def test_build_mixed_keeps_chinese_readable():
    """不转义成 \\uXXXX，方便直接在博客里看原始 JSON"""
    assert "第一段" in build_mixed([("text", "第一段")])
    assert "\\u" not in build_mixed([("text", "第一段")])


def test_build_mixed_is_compact_like_php_json_encode():
    """PHP json_encode 不输出空格，跟着它走，保证字节形态一致"""
    assert build_mixed([("text", "a")]) == '{"results":[{"type":"text","content":"a"}]}'


def test_build_mixed_tolerates_at_sign_in_content():
    """参考实现用 @ 当分隔符会在这里翻车，JSON 不会"""
    items = [("text", "邮箱 me@example.com"), ("text", "a->b")]
    payload = json.loads(build_mixed(items))
    assert payload["results"][0]["content"] == "邮箱 me@example.com"
    assert payload["results"][1]["content"] == "a->b"


def test_summarize_preview():
    items = [("text", "你好 世界"), ("image", "https://blog/u/1.jpg"), ("text", "#秘密")]
    preview = summarize(items)
    assert preview.startswith("你好 世界 | [图] | ")
    assert "秘密" in preview


def test_summarize_truncates():
    assert summarize([("text", "x" * 100)], limit=5).endswith("…")
