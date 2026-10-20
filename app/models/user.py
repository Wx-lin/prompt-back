"""
用户相关 ORM 模型：User（用户表）。

对应产品需求：PRD 3.6 用户体系与互动，支持 Google 登录为主、邮箱验证码为辅。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class User(Base):
    """
    用户表。

    网站用户使用 email/google_id；小程序用户使用独立 wechat_identities 关联。
    微信身份不会通过邮箱、unionid 或昵称自动绑定已有网站账号。
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    email: Mapped[str | None] = mapped_column(String(200), unique=True, comment="邮箱，邮箱登录时必填")
    google_id: Mapped[str | None] = mapped_column(String(100), unique=True, comment="Google 用户唯一 ID")

    # 历史预留字段，不用于小程序登录；openid 必须与 AppID 共同确定身份。
    wechat_openid: Mapped[str | None] = mapped_column(
        String(100), unique=True, comment="微信 openid，预留扩展位"
    )

    nickname: Mapped[str | None] = mapped_column(String(100), comment="昵称")
    avatar_url: Mapped[str | None] = mapped_column(String(500), comment="头像 URL")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
