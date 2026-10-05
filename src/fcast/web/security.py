"""Basic Auth and a same-origin check for state-changing requests."""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.types import ASGIApp, Receive, Scope, Send

from fcast.config import Settings

# auto_error=False: without a password configured the dashboard stays open (LAN only).
_basic = HTTPBasic(realm="FCast", auto_error=False)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ANONYMOUS = "anonymous"  # no password configured


def require_auth(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> str:
    """Basic Auth, unless no password is configured - then the dashboard is open."""
    settings: Settings = request.app.state.settings
    if settings.web_password is None or not settings.web_password.get_secret_value():
        return ANONYMOUS
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": 'Basic realm="FCast"'},
        )
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), settings.web_user.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.web_password.get_secret_value().encode("utf-8"),
    )
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="FCast"'},
        )
    return credentials.username


class SameOriginMiddleware:
    """Rejects cross-site form posts.

    Browsers attach Basic Auth credentials to cross-site requests automatically, so a foreign
    page could otherwise trigger actions. Requests without Origin/Referer (curl, tests) pass.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            headers = {
                key.decode("latin-1"): value.decode("latin-1") for key, value in scope["headers"]
            }
            source = headers.get("origin") or headers.get("referer")
            host = headers.get("host", "")
            # "null" is sent by sandboxed or file:// pages and never matches us.
            if source and (source == "null" or _host_of(source) != host):
                await _forbidden(send)
                return
        await self.app(scope, receive, send)


def _host_of(url: str) -> str:
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0]


async def _forbidden(send: Send) -> None:
    body = b"Cross-site request rejected"
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
