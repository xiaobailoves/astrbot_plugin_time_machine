"""图片处理 —— 压缩并转成主题要的 dataURL

只依赖 Pillow，不 import astrbot，方便离线跑单测。
真正「从聊天里把图片取出来」那一步交给 main.py 用框架自带的
Comp.Image.convert_to_base64()，这里只负责字节 → dataURL。
"""

import base64
import io
import logging

from PIL import Image as PILImage

logger = logging.getLogger("astrbot")

DEFAULT_MAX_EDGE = 2560
DEFAULT_QUALITY = 88
# 逐级降质，直到体积达标
QUALITY_LADDER = (88, 75, 60)


class MediaError(Exception):
    """图片认不出、取不到，或者压不小"""


def data_url(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def to_data_url(
    raw: bytes,
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_QUALITY,
    max_bytes: int = 0,
) -> str:
    """把图片字节压成 dataURL。

    PHP 默认 post_max_size = 8M，手机原图直接 base64 很容易超限；而超限时 PHP 会把
    整个 POST 丢空，主题只会回一个含糊的错误码 —— 所以这里必须先压再传。

    Args:
        max_bytes: dataURL 字符串的最大长度（近似等于它占的 POST 体积）。0 表示不限制。
    """
    if not raw:
        raise MediaError("图片内容是空的")

    try:
        img = PILImage.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        raise MediaError(f"认不出这张图片的格式（{type(e).__name__}）") from e

    if getattr(img, "is_animated", False):
        # 动图重新编码会丢帧，原样交出，只做体积检查
        mime = PILImage.MIME.get(img.format or "", "image/gif")
        url = data_url(raw, mime)
        _check_size(url, max_bytes)
        return url

    img = _flatten(img)
    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), PILImage.LANCZOS)

    ladder = (quality,) + tuple(q for q in QUALITY_LADDER if q < quality)
    last = ""
    for q in ladder:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        last = data_url(buf.getvalue())
        if not max_bytes or len(last) <= max_bytes:
            return last

    raise MediaError(
        f"压到 q={ladder[-1]} 仍有 {len(last) / 1048576:.1f}MB，"
        f"超过上限 {max_bytes / 1048576:.1f}MB"
    )


def _flatten(img: PILImage.Image) -> PILImage.Image:
    """统一成 RGB。带透明通道的先铺一层白底，免得转 JPEG 后变成黑块。"""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = PILImage.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _check_size(url: str, max_bytes: int) -> None:
    if max_bytes and len(url) > max_bytes:
        raise MediaError(
            f"压不小了：{len(url) / 1048576:.1f}MB，超过上限 {max_bytes / 1048576:.1f}MB"
        )
