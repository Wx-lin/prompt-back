"""Bounded, strict input for private character assets."""

import base64
import binascii
import io
import json

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

MAX_IMAGE_BYTES = 15 * 1024 * 1024


class CharacterAssetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(min_length=1, max_length=100)
    values: dict[StrictStr, StrictStr]
    template: StrictStr = Field(min_length=1, max_length=100)
    model: StrictStr = Field(min_length=1, max_length=100)
    preview_image_base64: StrictStr | None = Field(default=None, max_length=4 * ((MAX_IMAGE_BYTES + 2) // 3))
    preview_signature: StrictStr | None = Field(default=None, max_length=100_000)

    @field_validator("name", "template", "model")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Value cannot be blank")
        return value.strip()

    @field_validator("values")
    @classmethod
    def bounded_values(cls, value):
        if (
            len(value) > 300
            or any(len(k) > 100 or len(v) > 5000 for k, v in value.items())
            or len(json.dumps(value, ensure_ascii=False).encode()) > 100_000
        ):
            raise ValueError("Values too large")
        return value

    @field_validator("preview_image_base64")
    @classmethod
    def valid_png(cls, value):
        if value is None:
            return value
        try:
            raw = base64.b64decode(value, validate=True)
            if len(raw) > MAX_IMAGE_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Invalid image")
            with Image.open(io.BytesIO(raw)) as image:
                if (
                    image.format != "PNG"
                    or image.width * image.height > 16_000_000
                    or getattr(image, "is_animated", False)
                ):
                    raise ValueError("Invalid image dimensions")
                image.verify()
            # Verify checks structure and CRC but does not decompress IDAT data.
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
        except (
            binascii.Error,
            UnidentifiedImageError,
            OSError,
            EOFError,
            SyntaxError,
            Image.DecompressionBombError,
        ):
            raise ValueError("Invalid PNG") from None
        return value
