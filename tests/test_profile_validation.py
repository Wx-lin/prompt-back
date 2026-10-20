"""Avatar and nickname validation without external services."""

import io
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from app.schemas.auth import UserProfileUpdate
from app.services.user_profile import _owned_key, prepare_avatar


def png_bytes(size):
    stream = io.BytesIO()
    with Image.new("RGB", size, "red") as image:
        image.save(stream, "PNG")
    return stream.getvalue()


def test_more_than_four_megapixels_is_rejected_before_pixel_decode(monkeypatch):
    body = png_bytes((4001, 1000))

    def forbidden_decode(*args, **kwargs):
        pytest.fail("oversized image reached pixel decoding")

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", forbidden_decode)
    with pytest.raises(ValueError, match="尺寸"):
        prepare_avatar(body, "image/png")


def test_four_megapixel_boundary_is_resized_before_rgba_allocation(monkeypatch):
    body = png_bytes((4000, 1000))
    original_convert = Image.Image.convert
    rgba_sizes = []

    def observed_convert(self, mode=None, *args, **kwargs):
        if mode == "RGBA":
            rgba_sizes.append(self.size)
        return original_convert(self, mode, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", observed_convert)
    output = prepare_avatar(body, "image/png")
    assert rgba_sizes and all(max(size) <= 512 for size in rgba_sizes)
    with Image.open(io.BytesIO(output)) as avatar:
        assert avatar.size == (256, 256) and avatar.format == "JPEG"


def test_simultaneous_real_png_decodes_are_serialized(monkeypatch):
    body = png_bytes((800, 500))
    original_load = PngImagePlugin.PngImageFile.load
    state = {"active": 0, "maximum": 0, "calls": 0}
    lock = Lock()
    ready = Barrier(5)

    def observed_load(self, *args, **kwargs):
        with lock:
            state["active"] += 1
            state["calls"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        try:
            time.sleep(0.02)
            return original_load(self, *args, **kwargs)
        finally:
            with lock:
                state["active"] -= 1

    def prepare():
        ready.wait(timeout=5)
        return prepare_avatar(body, "image/png")

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", observed_load)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(prepare) for _ in range(4)]
        ready.wait(timeout=5)
        outputs = [future.result(timeout=10) for future in futures]
    assert state["calls"] >= 4 and state["maximum"] == 1
    for output in outputs:
        with Image.open(io.BytesIO(output)) as avatar:
            assert avatar.size == (256, 256) and avatar.format == "JPEG"


def test_downscaled_avatar_preserves_exif_orientation():
    image = Image.new("RGB", (800, 800), "red")
    image.paste("blue", (0, 400, 800, 800))
    exif = Image.Exif()
    exif[0x0112] = 6
    stream = io.BytesIO()
    image.save(stream, "JPEG", exif=exif)
    output = prepare_avatar(stream.getvalue(), "image/jpeg")
    with Image.open(io.BytesIO(output)) as avatar:
        left, right = avatar.getpixel((64, 128)), avatar.getpixel((192, 128))
        assert left[2] > 200 and left[0] < 50
        assert right[0] > 200 and right[2] < 50
        assert not avatar.getexif()


@pytest.mark.parametrize(
    "format,mime",
    [
        ("JPEG", "image/jpeg"),
        ("PNG", "image/png"),
        ("WEBP", "image/webp"),
        ("PNG", "application/octet-stream"),
    ],
)
def test_valid_avatar_is_square_jpeg_without_metadata(format, mime):
    image = Image.new("RGB", (100, 60), "red")
    exif = Image.Exif()
    exif[0x010E] = "private image comment"
    stream = io.BytesIO()
    image.save(stream, format, exif=exif, icc_profile=b"private color profile")
    output = prepare_avatar(stream.getvalue(), mime)
    with Image.open(io.BytesIO(output)) as result:
        assert result.size == (256, 256) and result.format == "JPEG"
        assert not result.getexif() and not result.info.get("icc_profile")
    assert b"private" not in output


def test_animated_webp_is_rejected():
    stream = io.BytesIO()
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "green")]
    frames[0].save(stream, "WEBP", save_all=True, append_images=frames[1:], duration=50)
    with pytest.raises(ValueError, match="静态"):
        prepare_avatar(stream.getvalue(), "image/webp")


@pytest.mark.parametrize("value", [None, 42, "名字\x00", "名字\u200b", "名" * 31, "  "])
def test_nickname_requires_trimmed_bounded_plain_unicode(value):
    with pytest.raises(ValidationError):
        UserProfileUpdate(nickname=value)


def test_unicode_nickname_boundary_and_server_fields():
    assert UserProfileUpdate(nickname="  " + "名" * 30 + "  ").nickname == "名" * 30
    with pytest.raises(ValidationError):
        UserProfileUpdate(nickname="名字", app_id="forged")


@pytest.mark.parametrize(
    "reference",
    [
        "cos-avatar://user-avatars/{user}/../image.jpg",
        "cos-avatar://user-avatars/{user}/" + "a" * 32 + ".jpg?signature=old",
        "cos-avatar://user-avatars/{user}/" + "a" * 32 + ".png",
        "https://evil.test/user-avatars/{user}/" + "a" * 32 + ".jpg",
    ],
)
def test_avatar_marker_rejects_traversal_signature_or_external_url(reference):
    user_id = uuid.uuid4()
    assert _owned_key(reference.format(user=user_id), user_id) is None
