"""Localhost-only HTTP server for browser-based translation review."""

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

from glk.application.translation_review_service import (
    TranslationReviewError,
    finalize_project_translation_review,
    get_project_translation_review_document,
    run_project_translation_qa,
    save_project_translation_review,
)


_MAX_REQUEST_BYTES = 8 * 1024 * 1024
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


class TranslationReviewHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer performs a reverse-DNS lookup here, which can block on
        # offline machines. The review server is localhost-only and needs no
        # public hostname.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        project: str | Path,
        workspace_root: str | Path,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.project = str(project)
        self.workspace_root = str(workspace_root)
        self.auth_token = secrets.token_urlsafe(32)
        self.mutation_lock = threading.Lock()

    @property
    def origin(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def review_url(self) -> str:
        return self.origin + "/"


class _TranslationReviewHandler(BaseHTTPRequestHandler):
    server: TranslationReviewHttpServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _host_is_local(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].casefold()
        return host in {"127.0.0.1", "localhost"}

    def _api_authorized(self) -> bool:
        if not self._host_is_local():
            return False
        if self.headers.get("X-GLK-Token") != self.server.auth_token:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {
            self.server.origin,
            self.server.origin.replace("127.0.0.1", "localhost"),
        }:
            return False
        return True

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

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"ok": False, "message": message})

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/favicon.ico":
            self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        if not self._host_is_local():
            self._send_error_json(HTTPStatus.FORBIDDEN, "Only localhost is allowed.")
            return
        if path == "/":
            template = (
                resources.files("glk.web")
                .joinpath("translation_review.html")
                .read_text(encoding="utf-8")
            )
            html = template.replace(
                "__GLK_TOKEN_JSON__",
                json.dumps(self.server.auth_token),
            ).encode("utf-8")
            self._send_bytes(HTTPStatus.OK, html, "text/html; charset=utf-8")
            return
        if path == "/api/review":
            if not self._api_authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "Invalid review session.")
                return
            try:
                document = get_project_translation_review_document(
                    project=self.server.project,
                    workspace_root=self.server.workspace_root,
                )
            except (TranslationReviewError, OSError, ValueError) as error:
                self._send_error_json(HTTPStatus.CONFLICT, str(error))
                return
            self._send_json(HTTPStatus.OK, document)
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def _read_request_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("application/json"):
            raise TranslationReviewError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise TranslationReviewError("Invalid Content-Length.") from error
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            raise TranslationReviewError("Request body size is invalid.")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TranslationReviewError("Request body must be valid UTF-8 JSON.") from error
        if not isinstance(value, dict):
            raise TranslationReviewError("Request body must be a JSON object.")
        return value

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/save", "/api/qa", "/api/finalize"}:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if not self._api_authorized():
            self._send_error_json(HTTPStatus.FORBIDDEN, "Invalid review session.")
            return
        try:
            body = self._read_request_json()
            review_hash = body.get("review_sha256")
            translations = body.get("translations")
            if not isinstance(review_hash, str):
                raise TranslationReviewError("review_sha256 is required.")
            if not isinstance(translations, dict):
                raise TranslationReviewError("translations must be an object.")
            with self.server.mutation_lock:
                document = save_project_translation_review(
                    project=self.server.project,
                    workspace_root=self.server.workspace_root,
                    translations=translations,
                    expected_review_sha256=review_hash,
                )
                if path == "/api/save":
                    response: dict[str, Any] = {"ok": True, "document": document}
                elif path == "/api/qa":
                    result = run_project_translation_qa(
                        project=self.server.project,
                        workspace_root=self.server.workspace_root,
                    )
                    response = {
                        "ok": result.passed,
                        "result": result.to_dict(),
                        "document": get_project_translation_review_document(
                            project=self.server.project,
                            workspace_root=self.server.workspace_root,
                        ),
                    }
                else:
                    result = finalize_project_translation_review(
                        project=self.server.project,
                        workspace_root=self.server.workspace_root,
                    )
                    response = {
                        "ok": result.valid,
                        "result": result.to_dict(),
                        "document": get_project_translation_review_document(
                            project=self.server.project,
                            workspace_root=self.server.workspace_root,
                        ),
                    }
        except (TranslationReviewError, OSError, ValueError) as error:
            status = (
                HTTPStatus.CONFLICT
                if "changed after this page was loaded" in str(error)
                else HTTPStatus.BAD_REQUEST
            )
            self._send_error_json(status, str(error))
            return
        self._send_json(HTTPStatus.OK, response)


def create_translation_review_server(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    port: int = 0,
) -> TranslationReviewHttpServer:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise TranslationReviewError("port must be between 0 and 65535.")
    get_project_translation_review_document(
        project=project,
        workspace_root=workspace_root,
    )
    return TranslationReviewHttpServer(
        ("127.0.0.1", port),
        _TranslationReviewHandler,
        project=project,
        workspace_root=workspace_root,
    )


def serve_translation_review(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    server = create_translation_review_server(
        project=project,
        workspace_root=workspace_root,
        port=port,
    )
    print(f"Translation review: {server.review_url}")
    print("Press Ctrl+C to stop the local review server.")
    if open_browser:
        webbrowser.open(server.review_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
