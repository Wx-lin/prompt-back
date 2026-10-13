"""Decode and strip metadata from untrusted uploaded images."""

import io
import warnings

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


def prepare_image(body: bytes, content_type: str) -> tuple[bytes, str]:
    if content_type not in FORMATS or not body or len(body) > MAX_IMAGE_BYTES:
        raise ValueError("请上传不超过 5 MiB 的 JPEG、PNG 或 WebP 图片")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as image:
                if image.format != FORMATS[content_type] or image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片格式不匹配或尺寸过大")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("请上传静态图片")
                image.load()
                clean = Image.new("RGB", image.size, "white")
                rgba = image.convert("RGBA")
                clean.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                clean.save(output, format="JPEG", quality=90)
                encoded = output.getvalue()
                if len(encoded) > MAX_IMAGE_BYTES:
                    raise ValueError("图片编码后过大，请减小尺寸")
                return encoded, "image/jpeg"
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("图片无法读取或尺寸过大") from exc
