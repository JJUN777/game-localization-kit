"""Shared localhost-only HTTP server security and JSON primitives."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from socketserver import TCPServer
import threading
from typing import Any
from urllib.parse import urlsplit

from glk.error_response import make_http_error_response


def local_security_headers(
    *,
    allow_blob_images: bool = False,
) -> dict[str, str]:
    """Build the shared browser security headers for one local UI."""
    image_sources = "'self' blob: data:" if allow_blob_images else "'self' data:"
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src 'self'; "
            f"img-src {image_sources}; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


LOCAL_SECURITY_HEADERS = local_security_headers()


def validate_local_port(
    port: object,
    *,
    error_type: type[ValueError] = ValueError,
) -> int:
    """Return a valid TCP port, rejecting bool and out-of-range values."""
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not 0 <= port <= 65535
    ):
        raise error_type("port must be between 0 and 65535.")
    return port


def validate_local_return_url(
    return_url: str | None,
    *,
    label: str,
) -> str | None:
    """Allow only credential-free localhost HTTP return URLs."""
    if return_url is None:
        return None
    parsed = urlsplit(return_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} return URL must be a local HTTP URL.")
    return return_url


class LocalHttpServer(ThreadingHTTPServer):
    """Threaded server with one localhost identity and session token."""

    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer performs a reverse-DNS lookup here, which can block on
        # offline machines. These servers bind only to localhost.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
    ) -> None:
        super().__init__(server_address, handler_class)
        self.auth_token = secrets.token_urlsafe(32)
        self.mutation_lock = threading.Lock()

    @property
    def origin(self) -> str:
        host, port = self.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else host
        return f"http://{host_text}:{port}"

    @property
    def root_url(self) -> str:
        return self.origin + "/"


class LocalHttpRequestHandler(BaseHTTPRequestHandler):
    """Common localhost authorization, headers, and JSON request handling."""

    server: LocalHttpServer
    request_error_type: type[ValueError] = ValueError
    security_headers: Mapping[str, str] = LOCAL_SECURITY_HEADERS

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _host_is_local(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].casefold()
        return host in {"127.0.0.1", "localhost"}

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or origin in {
            self.server.origin,
            self.server.origin.replace("127.0.0.1", "localhost"),
        }

    def _token_matches(self, supplied_token: object) -> bool:
        return isinstance(supplied_token, str) and secrets.compare_digest(
            supplied_token,
            self.server.auth_token,
        )

    def _api_authorized(self) -> bool:
        return (
            self._host_is_local()
            and self._origin_allowed()
            and self._token_matches(self.headers.get("X-GLK-Token"))
        )

    def _send_standard_headers(
        self,
        content_type: str,
        length: int,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name, value in self.security_headers.items():
            self.send_header(name, value)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)

    def _send_bytes(
        self,
        status: HTTPStatus,
        data: bytes,
        content_type: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._send_standard_headers(
            content_type,
            len(data),
            extra_headers=extra_headers,
        )
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: HTTPStatus, value: Any) -> None:
        data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8")

    def _send_error_json(
        self,
        status: HTTPStatus,
        detail: str | BaseException,
        *,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        self._send_json(
            status,
            make_http_error_response(
                status,
                detail,
                code=code,
                message=message,
            ).to_dict(),
        )

    def _read_request_json(
        self,
        *,
        max_bytes: int,
    ) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("application/json"):
            raise self.request_error_type(
                "Content-Type must be application/json."
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise self.request_error_type("Invalid Content-Length.") from error
        if length <= 0 or length > max_bytes:
            raise self.request_error_type("Request body size is invalid.")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise self.request_error_type(
                "Request body must be valid UTF-8 JSON."
            ) from error
        if not isinstance(value, dict):
            raise self.request_error_type(
                "Request body must be a JSON object."
            )
        return value
