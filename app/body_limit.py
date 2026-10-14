from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """Buffer at most limit bytes before handing a POST body to its parser."""

    def __init__(self, app, limit=4096):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        lengths = [v for k, v in scope["headers"] if k.lower() == b"content-length"]
        invalid = len(lengths) > 1 or any(
            not v.isdigit() or len(v) > 10 or int(v) > self.limit for v in lengths
        )
        body = bytearray()
        while not invalid:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.limit:
                invalid = True
                break
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        if invalid:
            return await JSONResponse({"detail": "Request too large."}, status_code=413)(
                scope, receive, send
            )
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
