"""Feature-scoped JSON errors and bounded request bodies, preserving legacy APIs."""

import logging
import uuid

from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.services.prompt_analysis.errors import AnalysisError

logger = logging.getLogger(__name__)
PRIVATE_PREFIXES = ("/api/prompt-assets", "/api/prompt-analyses", "/api/prompt-conversations")


def request_id(request):
    return request.scope.setdefault("state", {}).setdefault("prompt_request_id", uuid.uuid4().hex)


def success(request, data, *, status_code=200):
    return JSONResponse(
        {"success": True, "data": data, "error": None, "request_id": request_id(request)},
        status_code=status_code,
        headers={"Cache-Control": "private, no-store"},
    )


def error_response(status, code, message, rid):
    return JSONResponse(
        {"success": False, "data": None, "error": {"code": code, "message": message}, "request_id": rid},
        status_code=status,
        headers={"Cache-Control": "private, no-store", "X-Request-ID": rid},
    )


class PromptAnalysisRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            rid = request_id(request)
            try:
                return await handler(request)
            except AnalysisError as exc:
                return error_response(exc.status_code, exc.code, exc.message, rid)
            except RequestValidationError:
                return error_response(422, "INVALID_REQUEST", "请求参数不合法", rid)
            except HTTPException as exc:
                codes = {
                    400: "INVALID_REQUEST",
                    401: "UNAUTHENTICATED",
                    403: "FORBIDDEN",
                    404: "NOT_FOUND",
                    413: "PAYLOAD_TOO_LARGE",
                }
                messages = {
                    400: "请求格式不合法",
                    401: "请先登录",
                    403: "当前操作不可用",
                    404: "记录不存在",
                    413: "请求内容过大",
                }
                return error_response(
                    exc.status_code,
                    codes.get(exc.status_code, "REQUEST_FAILED"),
                    messages.get(exc.status_code, "请求未能完成"),
                    rid,
                )
            except Exception as exc:
                logger.error("prompt_api_failed request_id=%s type=%s", rid, type(exc).__name__)
                return error_response(500, "INTERNAL_ERROR", "服务暂时异常，请稍后重试", rid)

        return wrapped


class PromptAnalysisBoundary:
    """Read at most the feature limit before FastAPI parses multipart/JSON."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not any(
            path == p or path.startswith(p + "/") for p in PRIVATE_PREFIXES
        ):
            return await self.app(scope, receive, send)
        rid = scope.setdefault("state", {}).setdefault("prompt_request_id", uuid.uuid4().hex)

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = {**message, "headers": [*headers, (b"cache-control", b"private, no-store")]}
            await send(message)

        if scope["method"] not in {"POST", "PATCH", "PUT"}:
            return await self.app(scope, receive, private_send)
        maximum = (
            6 * 1024 * 1024 if path == "/api/prompt-assets" and scope["method"] == "POST" else 128 * 1024
        )
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
            if declared < 0:
                raise ValueError
        except ValueError:
            return await error_response(400, "INVALID_REQUEST", "请求头不合法", rid)(
                scope, receive, private_send
            )
        if declared > maximum:
            return await error_response(413, "PAYLOAD_TOO_LARGE", "请求内容过大", rid)(
                scope, receive, private_send
            )
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > maximum:
                return await error_response(413, "PAYLOAD_TOO_LARGE", "请求内容过大", rid)(
                    scope, receive, private_send
                )
            chunks.append(body)
            if not message.get("more_body", False):
                break
        pending = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": pending, "more_body": False}
            return await receive()

        return await self.app(scope, replay, private_send)
