"""Localhost-only HTTP server for the project dashboard."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import json
from pathlib import Path
import secrets
from socketserver import TCPServer
import threading
from typing import Any
from urllib.parse import urlsplit
import webbrowser

from glk.application.dashboard_service import get_dashboard_document
from glk.application.project_service import create_project as create_project_workspace
from glk.error_response import make_http_error_response
from glk.infrastructure.glossary_review_server import (
    create_glossary_review_server,
)
from glk.infrastructure.source_review_server import create_source_review_server
from glk.infrastructure.translation_review_server import (
    create_translation_review_server,
)


_MAX_REQUEST_BYTES = 64 * 1024
_REVIEW_TYPES = {"source", "glossary", "translation"}
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class DashboardError(ValueError):
    """Raised when the local dashboard cannot be started or used."""


class DashboardHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        workspace_root: str | Path,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.workspace_root = str(workspace_root)
        self.auth_token = secrets.token_urlsafe(32)
        self.mutation_lock = threading.Lock()
        self._review_lock = threading.Lock()
        self._review_servers: dict[
            tuple[str, str], tuple[ThreadingHTTPServer, threading.Thread]
        ] = {}

    @property
    def origin(self) -> str:
        host, port = self.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else host
        return f"http://{host_text}:{port}"

    @property
    def dashboard_url(self) -> str:
        return self.origin + "/"

    def open_review(self, project_id: str, review_type: str) -> str:
        if review_type not in _REVIEW_TYPES:
            raise DashboardError("Unknown review type.")
        if not project_id.strip():
            raise DashboardError("project_id is required.")

        key = (project_id, review_type)
        with self._review_lock:
            existing = self._review_servers.get(key)
            if existing is not None and existing[1].is_alive():
                return str(getattr(existing[0], "review_url"))
            if existing is not None:
                existing[0].server_close()
                self._review_servers.pop(key, None)

            factories = {
                "source": create_source_review_server,
                "glossary": create_glossary_review_server,
                "translation": create_translation_review_server,
            }
            review_server = factories[review_type](
                project=project_id,
                workspace_root=self.workspace_root,
                port=0,
            )
            review_thread = threading.Thread(
                target=review_server.serve_forever,
                name=f"glk-{review_type}-review-{project_id}",
                daemon=True,
            )
            review_thread.start()
            self._review_servers[key] = (review_server, review_thread)
            return str(getattr(review_server, "review_url"))

    def close_review_servers(self) -> None:
        with self._review_lock:
            running = list(self._review_servers.values())
            self._review_servers.clear()
        for review_server, review_thread in running:
            review_server.shutdown()
            review_server.server_close()
            review_thread.join(timeout=2)

    def server_close(self) -> None:
        self.close_review_servers()
        super().server_close()


class _DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHttpServer

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

    def _api_authorized(self) -> bool:
        return (
            self._host_is_local()
            and self._origin_allowed()
            and self.headers.get("X-GLK-Token") == self.server.auth_token
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        data: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
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
    ) -> None:
        self._send_json(
            status,
            make_http_error_response(status, detail, code=code).to_dict(),
        )

    def _read_request_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("application/json"):
            raise DashboardError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise DashboardError("Invalid Content-Length.") from error
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            raise DashboardError("Request body size is invalid.")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DashboardError(
                "Request body must be valid UTF-8 JSON."
            ) from error
        if not isinstance(value, dict):
            raise DashboardError("Request body must be a JSON object.")
        return value

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/favicon.ico":
            self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        if not self._host_is_local():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Only localhost is allowed.",
            )
            return
        if path == "/":
            template = (
                resources.files("glk.web")
                .joinpath("dashboard.html")
                .read_text(encoding="utf-8")
            )
            html = template.replace(
                "__GLK_TOKEN_JSON__",
                json.dumps(self.server.auth_token),
            ).encode("utf-8")
            self._send_bytes(
                HTTPStatus.OK,
                html,
                "text/html; charset=utf-8",
            )
            return
        if path == "/api/dashboard":
            if not self._api_authorized():
                self._send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "Invalid review session.",
                )
                return
            try:
                document = get_dashboard_document(self.server.workspace_root)
            except (OSError, TypeError, ValueError) as error:
                self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    error,
                    code="DASHBOARD_SERVER_FAILED",
                )
                return
            self._send_json(HTTPStatus.OK, document)
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/projects", "/api/review/open"}:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if not self._api_authorized():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Invalid review session.",
            )
            return
        try:
            request = self._read_request_json()
            if path == "/api/projects":
                name = request.get("name")
                project_id = request.get("project_id")
                if not isinstance(name, str) or not name.strip():
                    raise DashboardError("Project name is required.")
                if not isinstance(project_id, str) or not project_id.strip():
                    raise DashboardError("Project ID is required.")
                normalized_id = project_id.strip()
                with self.server.mutation_lock:
                    location = create_project_workspace(
                        name=name.strip(),
                        project_id=normalized_id,
                        workspace_root=self.server.workspace_root,
                    )
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "ok": True,
                        "project": {
                            "project_id": location.manifest.project_id,
                            "name": location.manifest.name,
                            "path": str(location.path),
                        },
                    },
                )
                return
            project_id = request.get("project_id")
            review_type = request.get("review_type")
            if not isinstance(project_id, str) or not isinstance(review_type, str):
                raise DashboardError(
                    "project_id and review_type must be strings."
                )
            url = self.server.open_review(project_id, review_type)
        except (DashboardError, OSError, TypeError, ValueError) as error:
            code = (
                "PROJECT_INIT_FAILED"
                if path == "/api/projects"
                else "INVALID_REQUEST"
            )
            self._send_error_json(HTTPStatus.BAD_REQUEST, error, code=code)
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "url": url})


def create_dashboard_server(
    *,
    workspace_root: str | Path = "workspaces",
    port: int = 0,
) -> DashboardHttpServer:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise DashboardError("port must be between 0 and 65535.")
    get_dashboard_document(workspace_root)
    return DashboardHttpServer(
        ("127.0.0.1", port),
        _DashboardHandler,
        workspace_root=workspace_root,
    )


def serve_dashboard(
    *,
    workspace_root: str | Path = "workspaces",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    server = create_dashboard_server(
        workspace_root=workspace_root,
        port=port,
    )
    print(f"GLK dashboard: {server.dashboard_url}")
    print("Press Ctrl+C to stop the local dashboard.")
    if open_browser:
        webbrowser.open(server.dashboard_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
