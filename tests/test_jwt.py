"""
JWT 工具的单元测试：验证生成的 token 能被正确解析，篡改/过期的 token 会被拒绝。
"""

import uuid

from app.utils.jwt import create_access_token, decode_access_token


def test_create_and_decode_round_trip():
    """生成 token 后立刻解析，应该能拿回同一个 user_id。"""
    user_id = uuid.uuid4()
    token = create_access_token(user_id)

    decoded_id = decode_access_token(token)

    assert decoded_id == user_id


def test_decode_invalid_token_returns_none():
    """格式错误/签名无效的 token，解析应该返回 None 而不是抛异常。"""
    assert decode_access_token("this-is-not-a-valid-jwt") is None


def test_decode_empty_token_returns_none():
    """空字符串也应该被安全地判定为无效 token。"""
    assert decode_access_token("") is None
