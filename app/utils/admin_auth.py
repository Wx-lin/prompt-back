"""
后台管理接口的鉴权依赖。

MVP 阶段只有运营方（也就是你自己）一个人用后台管理功能，不需要完整的用户角色
体系（RBAC），用一个固定密钥校验请求头 `X-Admin-Key` 即可，足够安全、足够简单。

使用方式：在后台管理路由的每个接口上加 `Depends(verify_admin_key)`。
"""

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def verify_admin_key(x_admin_key: str | None = Header(None)) -> None:
    """
    校验请求头 `X-Admin-Key` 是否和 `.env` 里配置的 `ADMIN_SECRET_KEY` 一致。

    请求头缺失或不匹配均抛 401（而不是让 FastAPI 对"缺少必填请求头"返回 422，
    422 会暴露"这个接口需要哪个请求头"的内部细节，统一成 401 更安全）。
    调用方（Swagger UI / 内部管理脚本）需要在请求头里带上这个密钥。
    """
    settings = get_settings()
    if x_admin_key is None or x_admin_key != settings.admin_secret_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员密钥不正确")
