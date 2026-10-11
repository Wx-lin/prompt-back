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

    email 和 google_id 都允许为空，但二者至少有一个必须存在（由应用层逻辑保证，
    不在数据库层强制约束，避免过度限制未来扩展其他登录方式，如微信扫码登录）。
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    email: Mapped[str | None] = mapped_column(String(200), unique=True, comment="邮箱，邮箱登录时必填")
    google_id: Mapped[str | None] = mapped_column(String(100), unique=True, comment="Google 用户唯一 ID")

    # 预留字段：微信扫码登录（需企业主体 + 认证费 + 已备案域名，MVP 暂不启用，见后端技术选型.md 第十节）
    wechat_openid: Mapped[str | None] = mapped_column(
        String(100), unique=True, comment="微信 openid，预留扩展位"
    )

    nickname: Mapped[str | None] = mapped_column(String(100), comment="昵称")
    avatar_url: Mapped[str | None] = mapped_column(String(500), comment="头像 URL")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
