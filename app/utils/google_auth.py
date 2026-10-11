"""
Google OAuth id_token 验证工具。
"""

from google.auth.exceptions import TransportError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.config import get_settings


class GoogleTokenInvalidError(Exception):
    """Google id_token 验证失败时抛出。"""


class GoogleVerificationUnavailableError(Exception):
    """Google verification service could not be reached."""


class _BoundedGoogleRequest(google_requests.Request):
    def __call__(self, url, method="GET", body=None, headers=None, timeout=10, **kwargs):
        return super().__call__(
            url, method=method, body=body, headers=headers, timeout=min(timeout or 10, 10), **kwargs
        )


def verify_google_id_token(token: str) -> dict:
    """
    验证前端传来的 Google id_token，成功返回包含 sub（Google 用户唯一ID）、
    email、name、picture 等字段的字典；失败抛出 GoogleTokenInvalidError。
    """
    settings = get_settings()
    try:
        return id_token.verify_oauth2_token(token, _BoundedGoogleRequest(), settings.google_client_id)
    except TransportError as exc:
        raise GoogleVerificationUnavailableError("Google verification service unavailable") from exc
    except ValueError as exc:
        raise GoogleTokenInvalidError(str(exc)) from exc
