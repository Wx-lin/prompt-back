"""
认证业务逻辑：查找或创建用户（登录即注册），统一两种登录方式的用户落库逻辑。
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def get_or_create_user_by_email(db: AsyncSession, email: str) -> User:
    """邮箱验证码登录：按邮箱查找用户，不存在则创建（登录即注册，不需要单独的注册流程）。"""
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=email)
        db.add(user)

    user.last_login_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(user)
    return user


async def get_or_create_user_by_google(
    db: AsyncSession, google_id: str, email: str | None, nickname: str | None, avatar_url: str | None
) -> User:
    """Google 登录：按 google_id 查找用户，不存在则按邮箱查找并绑定，都没有则新建，并同步最新的昵称/头像。"""
    result = await db.execute(select(User).where(User.google_id == google_id))
    user = result.scalar_one_or_none()

    if user is None and email:
        # 邮箱已通过其他方式注册，绑定 google_id
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

    if user is None:
        user = User(google_id=google_id, email=email, nickname=nickname, avatar_url=avatar_url)
        db.add(user)
    else:
        # 补绑 google_id，并同步最新的昵称/头像
        if user.google_id is None:
            user.google_id = google_id
        user.nickname = nickname
        user.avatar_url = avatar_url

    user.last_login_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(user)
    return user
