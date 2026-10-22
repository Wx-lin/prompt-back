import hashlib
import io
import os
from dataclasses import FrozenInstanceError

import pytest
from PIL import Image

from app.services.prompt_analysis_images import prepare_analysis_image


def encoded_image(format="PNG", size=(24, 16), *, mode="RGB", **save_options):
    stream = io.BytesIO()
    color = (255, 0, 0, 0) if mode == "RGBA" else "red"
    Image.new(mode, size, color).save(stream, format=format, **save_options)
    return stream.getvalue()


@pytest.mark.parametrize(
    "format,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")]
)
def test_prepares_only_normalized_jpegs_with_hash_of_stored_image(format, mime):
    prepared = prepare_analysis_image(encoded_image(format), mime)
    assert (prepared.width, prepared.height) == (24, 16)
    assert prepared.sha256 == hashlib.sha256(prepared.image_bytes).hexdigest()
    for body in (prepared.image_bytes, prepared.thumbnail_bytes):
        with Image.open(io.BytesIO(body)) as image:
            image.load()
            assert image.format == "JPEG"
            assert image.mode == "RGB"
            assert not image.getexif()
            assert "icc_profile" not in image.info
    with pytest.raises(FrozenInstanceError):
        prepared.width = 1


@pytest.mark.parametrize(
    "mime,body",
    [
        ("image/svg+xml", b"<svg/>"),
        ("image/png", b""),
        ("image/png", b"broken image"),
        ("image/jpeg", encoded_image()),
        ("image/png", encoded_image("BMP")),
        ("image/png", b"x" * (5 * 1024 * 1024 + 1)),
        ("image/jpeg", encoded_image("JPEG")[:-10]),
    ],
)
def test_rejects_invalid_images_before_normalizing(mime, body):
    with pytest.raises(ValueError):
        prepare_analysis_image(body, mime)


def test_rejects_decoded_images_over_twenty_megapixels():
    with pytest.raises(ValueError):
        prepare_analysis_image(encoded_image(size=(5001, 4000)), "image/png")


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("WEBP", "image/webp")])
def test_rejects_multiframe_images(format, mime):
    stream = io.BytesIO()
    Image.new("RGB", (24, 16), "red").save(
        stream,
        format=format,
        save_all=True,
        append_images=[Image.new("RGB", (24, 16), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(ValueError, match="静态"):
        prepare_analysis_image(stream.getvalue(), mime)


def test_corrects_exif_direction_before_recording_dimensions_and_removes_metadata():
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private original text"
    prepared = prepare_analysis_image(encoded_image("JPEG", exif=exif), "image/jpeg")
    assert (prepared.width, prepared.height) == (16, 24)
    with Image.open(io.BytesIO(prepared.image_bytes)) as image:
        assert image.size == (16, 24)
        assert not image.getexif()


def test_transparent_regions_are_white():
    prepared = prepare_analysis_image(encoded_image(mode="RGBA"), "image/png")
    with Image.open(io.BytesIO(prepared.image_bytes)) as image:
        assert min(image.getpixel((0, 0))) >= 250


def test_standard_image_and_thumbnail_are_bounded_without_upscaling():
    prepared = prepare_analysis_image(encoded_image(size=(3072, 1536)), "image/png")
    assert (prepared.width, prepared.height) == (1536, 768)
    with Image.open(io.BytesIO(prepared.thumbnail_bytes)) as image:
        assert image.size == (320, 160)
    assert len(prepared.image_bytes) <= 2 * 1024 * 1024


def test_high_entropy_input_meets_encoded_size_limit():
    stream = io.BytesIO()
    Image.frombytes("RGB", (1536, 1536), os.urandom(1536 * 1536 * 3)).save(
        stream,
        format="JPEG",
        quality=95,
    )
    prepared = prepare_analysis_image(stream.getvalue(), "image/jpeg")
    assert len(prepared.image_bytes) <= 2 * 1024 * 1024


def test_pillow_decompression_bomb_warning_is_rejected(monkeypatch):
    body = encoded_image(size=(24, 16))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 300)
    with pytest.raises(ValueError):
        prepare_analysis_image(body, "image/png")
