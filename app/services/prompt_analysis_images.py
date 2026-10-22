"""Decode untrusted images into bounded metadata-free private JPEG assets."""

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_NORMALIZED_BYTES = 2 * 1024 * 1024
MAX_IMAGE_EDGE = 1536
THUMBNAIL_EDGE = 320
_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


@dataclass(frozen=True)
class PreparedImage:
    image_bytes: bytes
    thumbnail_bytes: bytes
    width: int
    height: int
    sha256: str


def _encode_jpeg(image: Image.Image) -> bytes:
    for quality in (90, 82, 74, 66, 58, 50):
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        encoded = output.getvalue()
        if len(encoded) <= MAX_NORMALIZED_BYTES:
            return encoded
    raise ValueError("图片编码后过大，请减小尺寸")


def _normalize(image: Image.Image) -> Image.Image:
    oriented = ImageOps.exif_transpose(image)
    bounded = ImageOps.contain(
        oriented,
        (min(oriented.width, MAX_IMAGE_EDGE), min(oriented.height, MAX_IMAGE_EDGE)),
        Image.Resampling.LANCZOS,
    )
    rgba = bounded.convert("RGBA")
    white = Image.new("RGBA", rgba.size, "white")
    flattened = Image.alpha_composite(white, rgba).convert("RGB")
    return Image.frombytes("RGB", flattened.size, flattened.tobytes())


def prepare_analysis_image(body: bytes, content_type: str) -> PreparedImage:
    """Validate actual format, dimensions and complete decode before normalization."""
    if content_type not in _FORMATS or not body or len(body) > MAX_IMAGE_BYTES:
        raise ValueError("请上传不超过 5 MiB 的 JPEG、PNG 或 WebP 图片")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as image:
                if image.format != _FORMATS[content_type]:
                    raise ValueError("图片格式与 MIME 不匹配")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片尺寸不能超过 2000 万像素")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("请上传静态图片")
                image.verify()
            with Image.open(io.BytesIO(body)) as image:
                image.load()
                clean = _normalize(image)
            encoded = _encode_jpeg(clean)
            thumbnail = ImageOps.contain(
                clean,
                (min(clean.width, THUMBNAIL_EDGE), min(clean.height, THUMBNAIL_EDGE)),
                Image.Resampling.LANCZOS,
            )
            return PreparedImage(
                image_bytes=encoded,
                thumbnail_bytes=_encode_jpeg(thumbnail),
                width=clean.width,
                height=clean.height,
                sha256=hashlib.sha256(encoded).hexdigest(),
            )
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("图片无法完整读取或尺寸过大") from exc
