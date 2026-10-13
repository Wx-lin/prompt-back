"""Bound direct API upload bodies before multipart parsing can spool to disk."""

from starlette.responses import JSONResponse

from app.services.submission_images import MAX_IMAGE_BYTES


class SubmissionBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and path.startswith(("/api/submissions", "/api/admin/submissions")):
            original_send = send

            async def private_send(message):
                if message["type"] == "http.response.start":
                    message = {
                        **message,
                        "headers": [*message.get("headers", []), (b"cache-control", b"private, no-store")],
                    }
                await original_send(message)

            send = private_send
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or path not in {"/api/submissions", "/api/submissions/upload"}
        ):
            return await self.app(scope, receive, send)
        maximum = MAX_IMAGE_BYTES + 65536 if path.endswith("/upload") else 131072
        messages = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > maximum:
                return await JSONResponse({"detail": "请求内容过大"}, status_code=413)(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        iterator = iter(messages)

        async def replay():
            message = next(iterator, None)
            return message if message is not None else await receive()

        await self.app(scope, replay, send)
