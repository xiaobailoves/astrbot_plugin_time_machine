"""图片处理单测：压缩、透明底、体积上限"""

import base64
import io
import os

import pytest
from PIL import Image as PILImage

from src.media import MediaError, to_data_url


def decode(url: str) -> bytes:
    return base64.b64decode(url.split(",", 1)[1])


def make_jpeg(size=(1200, 900), color=(120, 30, 200)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_output_is_jpeg_data_url():
    url = to_data_url(make_jpeg())
    assert url.startswith("data:image/jpeg;base64,")
    assert PILImage.open(io.BytesIO(decode(url))).format == "JPEG"


def test_large_image_is_downscaled():
    url = to_data_url(make_jpeg((4000, 3000)), max_edge=2560)
    img = PILImage.open(io.BytesIO(decode(url)))
    assert max(img.size) == 2560
    assert img.size == (2560, 1920)  # 保持比例


def test_small_image_keeps_size():
    url = to_data_url(make_jpeg((300, 200)), max_edge=2560)
    assert PILImage.open(io.BytesIO(decode(url))).size == (300, 200)


def test_transparency_becomes_white_not_black():
    """带 alpha 的图铺白底，否则转 JPEG 后透明区会变黑块"""
    buf = io.BytesIO()
    PILImage.new("RGBA", (64, 64), (255, 0, 0, 0)).save(buf, format="PNG")

    img = PILImage.open(io.BytesIO(decode(to_data_url(buf.getvalue())))).convert("RGB")
    r, g, b = img.getpixel((32, 32))
    assert min(r, g, b) > 250


def test_opaque_png_keeps_color():
    buf = io.BytesIO()
    PILImage.new("RGB", (64, 64), (10, 200, 30)).save(buf, format="PNG")

    img = PILImage.open(io.BytesIO(decode(to_data_url(buf.getvalue())))).convert("RGB")
    r, g, b = img.getpixel((32, 32))
    assert g > 180 and r < 60 and b < 60


def test_oversized_image_is_rejected_or_shrunk():
    """纯噪声图几乎压不动：给了很小的上限就应该明确报错，而不是悄悄发出去"""
    raw = PILImage.frombytes("RGB", (600, 600), os.urandom(600 * 600 * 3))
    buf = io.BytesIO()
    raw.save(buf, format="PNG")

    with pytest.raises(MediaError) as exc:
        to_data_url(buf.getvalue(), max_bytes=2000)
    assert "超过上限" in str(exc.value)


def test_quality_ladder_shrinks_output():
    raw = PILImage.frombytes("RGB", (400, 400), os.urandom(400 * 400 * 3))
    buf = io.BytesIO()
    raw.save(buf, format="PNG")
    data = buf.getvalue()

    big = len(to_data_url(data, quality=88))
    small = len(to_data_url(data, quality=60))
    assert small < big


def test_non_image_is_rejected():
    with pytest.raises(MediaError) as exc:
        to_data_url(b"this is definitely not an image")
    assert "认不出" in str(exc.value)


def test_empty_input_is_rejected():
    with pytest.raises(MediaError):
        to_data_url(b"")


def make_animated_gif() -> bytes:
    """两帧颜色不同的 GIF。纯色 P 模式图会被编码器合并成一帧，所以必须真的不一样。"""
    frames = [
        PILImage.new("RGB", (32, 32), (255, 0, 0)),
        PILImage.new("RGB", (32, 32), (0, 0, 255)),
    ]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buf.getvalue()


def test_gif_fixture_is_actually_animated():
    assert PILImage.open(io.BytesIO(make_animated_gif())).is_animated


def test_animated_gif_is_passed_through_untouched():
    """动图重新编码会丢帧，必须原样送出"""
    original = make_animated_gif()

    url = to_data_url(original)
    assert url.startswith("data:image/gif;base64,")
    assert decode(url) == original


def test_animated_gif_respects_size_limit():
    with pytest.raises(MediaError):
        to_data_url(make_animated_gif(), max_bytes=10)
