"""
认证与用户相关 Pydantic Schema。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SendCodeRequest(BaseModel):
    """发送邮箱验证码请求体。"""

    email: EmailStr


class VerifyCodeRequest(BaseModel):
    """验证邮箱验证码并登录/注册请求体。"""

    email: EmailStr
    code: str


class GoogleLoginRequest(BaseModel):
    """Google 登录请求体，id_token 由前端 Google 登录 SDK 返回后转发给后端验证。"""

    id_token: str


class WeChatLoginRequest(BaseModel):
    """仅接收 wx.login 的临时 code，身份与 AppID 均由服务端确认。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_-]+$")


class TokenResponse(BaseModel):
    """登录成功后的令牌响应，由网站或小程序客户端按各自会话策略保存。"""

    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    """当前用户信息，对应 GET /api/auth/me。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    created_at: datetime
