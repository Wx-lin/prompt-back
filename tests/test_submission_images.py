import io

import pytest
from PIL import Image

from app.services.submission_images import MAX_IMAGE_BYTES, prepare_image


def image_bytes(format="PNG", size=(20, 20)):
    stream = io.BytesIO()
    Image.new("RGB", size, "red").save(stream, format=format)
    return stream.getvalue()


@pytest.mark.parametrize(
    "mime,body",
    [
        ("image/svg+xml", b"<svg/>"),
        ("image/png", b"not an image"),
        ("image/jpeg", image_bytes()),
        ("image/png", b"x" * (MAX_IMAGE_BYTES + 1)),
    ],
)
def test_invalid_upload_is_rejected(mime, body):
    with pytest.raises(ValueError):
        prepare_image(body, mime)


def test_large_pixel_image_is_rejected():
    with pytest.raises(ValueError):
        prepare_image(image_bytes(size=(5000, 5000)), "image/png")


@pytest.mark.parametrize(
    "format,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")]
)
def test_valid_image_is_decoded_and_metadata_removed(format, mime):
    body, actual = prepare_image(image_bytes(format=format), mime)
    assert actual == "image/jpeg"
    with Image.open(io.BytesIO(body)) as result:
        assert result.format == "JPEG"
        assert result.size == (20, 20)
        assert not result.getexif()
