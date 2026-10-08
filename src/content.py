"""内容格式化 —— 与主题端约定的三种内容形态（纯函数，便于离线单测）

约定来自两份现成实现：
  - 单图：<img src='URL' />（time_machine/js/oper.js:193）
  - 私密：# 开头包成 [secret]...[/secret]（oper.js:277-281、cross.php:235-239）
  - 连续多条：mixed_talk 的 JSON（wechat_for_handsome/cross.php:190）
"""

import json

SECRET_OPEN = "[secret]"
SECRET_CLOSE = "[/secret]"


def apply_secret(text: str) -> str:
    """# 开头的说说转成私密内容（只有自己看得到）"""
    if text.startswith("#"):
        return f"{SECRET_OPEN}{text[1:]}{SECRET_CLOSE}"
    return text


def image_tag(url: str) -> str:
    """拼成 <img> 交给主题渲染。注意是单引号，与 Chrome 插件保持一致。"""
    return f"<img src='{url}' />"


def build_mixed(items: list[tuple[str, str]]) -> str:
    """连续多条 → mixed_talk 的内容体。

    items 为 (type, content) 的有序列表，type ∈ {"text", "image"}。
    图片用 type=image + 图片地址，这条路径照搬微信服务端（唯一实现了混排的实现）。
    """
    payload = {"results": [{"type": t, "content": c} for t, c in items]}
    # 紧凑分隔符，与 PHP json_encode 的输出保持一致的字节形态
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def summarize(items: list[tuple[str, str]], limit: int = 20) -> str:
    """把缓冲内容压成一行预览，给聊天里回显用"""
    parts = []
    for item_type, content in items:
        if item_type == "image":
            parts.append("[图]")
        else:
            text = content.replace(SECRET_OPEN, "[私密]").replace(SECRET_CLOSE, "")
            text = " ".join(text.split())
            parts.append(text[:limit] + ("…" if len(text) > limit else ""))
    return " | ".join(parts)
