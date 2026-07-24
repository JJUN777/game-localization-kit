"""Localhost-only HTTP server for visual source review."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import json
import mimetypes
from pathlib import Path
import secrets
from socketserver import TCPServer
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit
import webbrowser

from glk.application.project_service import load_project
from glk.application.source_review_service import (
    SourceReviewError,
    finalize_project_source_review,
    get_project_source_review_document,
    save_project_source_review,
)
from glk.domain.workspace import WorkspacePaths, is_pdf_source_file
from glk.error_response import make_http_error_response


_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' blob: data:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class SourceReviewHttpServer(ThreadingHTTPServer):
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


class _SourceReviewHandler(BaseHTTPRequestHandler):
    server: SourceReviewHttpServer

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

    def _asset_authorized(self, query: dict[str, list[str]]) -> bool:
        return (
            self._host_is_local()
            and self._origin_allowed()
            and query.get("token", [""])[0] == self.server.auth_token
        )

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)

    def _send_bytes(
        self, status: HTTPStatus, data: bytes, content_type: str
    ) -> None:
        self.send_response(status)
        self._headers(content_type, len(data))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: HTTPStatus, value: Any) -> None:
        self._send_bytes(
            status,
            (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json(
            status,
            make_http_error_response(status, message).to_dict(),
        )

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "Source asset not found.")
            return
        file_size = path.stat().st_size
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            try:
                start_text, end_text = range_header[6:].split("-", 1)
                start = int(start_text) if start_text else 0
                end = int(end_text) if end_text else end
                if start < 0 or end < start or end >= file_size:
                    raise ValueError
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return
        length = end - start + 1
        guessed = content_type or mimetypes.guess_type(path.name)[0]
        self.send_response(status)
        self._headers(guessed or "application/octet-stream", length)
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        with path.open("rb") as file:
            file.seek(start)
            remaining = length
            while remaining:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _document(self) -> dict[str, Any]:
        return get_project_source_review_document(
            project=self.server.project,
            workspace_root=self.server.workspace_root,
        )

    def _group_asset(self, group_id: str) -> Path:
        document = self._document()
        group = next(
            (value for value in document["groups"] if value["id"] == group_id),
            None,
        )
        if group is None:
            raise SourceReviewError("Unknown source review group.")
        location = load_project(self.server.project, self.server.workspace_root)
        paths = WorkspacePaths(location.path)
        if group["source_type"] == "pdf":
            return paths.pdf_pages / f"page_{int(group['page']):03d}.png"
        candidate = (location.path / group["source_file"]).resolve()
        try:
            candidate.relative_to(location.path.resolve())
        except ValueError as error:
            raise SourceReviewError("Unsafe image source path.") from error
        return candidate

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/favicon.ico":
            self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        if not self._host_is_local():
            self._send_error_json(HTTPStatus.FORBIDDEN, "Only localhost is allowed.")
            return
        if path == "/":
            template = (
                resources.files("glk.web")
                .joinpath("source_review.html")
                .read_text(encoding="utf-8")
            )
            html = template.replace(
                "__GLK_TOKEN_JSON__", json.dumps(self.server.auth_token)
            ).encode("utf-8")
            self._send_bytes(HTTPStatus.OK, html, "text/html; charset=utf-8")
            return
        if path == "/api/review":
            if not self._api_authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "Invalid review session.")
                return
            try:
                self._send_json(HTTPStatus.OK, self._document())
            except (SourceReviewError, OSError, ValueError) as error:
                self._send_error_json(HTTPStatus.CONFLICT, str(error))
            return
        if path == "/api/source-image":
            if not self._asset_authorized(query):
                self._send_error_json(HTTPStatus.FORBIDDEN, "Invalid review session.")
                return
            try:
                self._send_file(self._group_asset(query.get("group", [""])[0]))
            except (SourceReviewError, OSError, ValueError) as error:
                self._send_error_json(HTTPStatus.NOT_FOUND, str(error))
            return
        if path == "/api/original-pdf":
            if not self._asset_authorized(query):
                self._send_error_json(HTTPStatus.FORBIDDEN, "Invalid review session.")
                return
            try:
                location = load_project(
                    self.server.project, self.server.workspace_root
                )
                source_file = location.manifest.source_file
                if not is_pdf_source_file(source_file):
                    raise SourceReviewError("This project has no registered PDF.")
                self._send_file(
                    location.path / str(source_file), "application/pdf"
                )
            except (SourceReviewError, OSError, ValueError) as error:
                self._send_error_json(HTTPStatus.NOT_FOUND, str(error))
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def _read_request_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("application/json"):
            raise SourceReviewError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise SourceReviewError("Invalid Content-Length.") from error
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            raise SourceReviewError("Request body size is invalid.")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourceReviewError("Request body must be valid UTF-8 JSON.") from error
        if not isinstance(value, dict):
            raise SourceReviewError("Request body must be a JSON object.")
        return value

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/save", "/api/validate", "/api/finalize"}:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if not self._api_authorized():
            self._send_error_json(HTTPStatus.FORBIDDEN, "Invalid review session.")
            return
        try:
            body = self._read_request_json()
            review_hash = body.get("review_sha256")
            blocks = body.get("blocks")
            allow_token_changes = body.get("allow_token_changes", False)
            if not isinstance(review_hash, str):
                raise SourceReviewError("review_sha256 is required.")
            if not isinstance(blocks, list):
                raise SourceReviewError("blocks must be a list.")
            if not isinstance(allow_token_changes, bool):
                raise SourceReviewError("allow_token_changes must be true or false.")
            with self.server.mutation_lock:
                document = save_project_source_review(
                    project=self.server.project,
                    workspace_root=self.server.workspace_root,
                    blocks=blocks,
                    expected_review_sha256=review_hash,
                )
                if path == "/api/save":
                    response: dict[str, Any] = {"ok": True, "document": document}
                else:
                    result = finalize_project_source_review(
                        project=self.server.project,
                        workspace_root=self.server.workspace_root,
                        allow_token_changes=allow_token_changes,
                        dry_run=path == "/api/validate",
                    )
                    response = {
                        "ok": True,
                        "result": result.to_dict(),
                        "document": self._document(),
                    }
            self._send_json(HTTPStatus.OK, response)
        except SourceReviewError as error:
            status = (
                HTTPStatus.CONFLICT
                if "changed after" in str(error) or "stale" in str(error)
                else HTTPStatus.BAD_REQUEST
            )
            self._send_error_json(status, str(error))
        except (OSError, ValueError) as error:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))


def create_source_review_server(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    port: int = 0,
) -> SourceReviewHttpServer:
    get_project_source_review_document(
        project=project, workspace_root=workspace_root
    )
    return SourceReviewHttpServer(
        ("127.0.0.1", port),
        _SourceReviewHandler,
        project=project,
        workspace_root=workspace_root,
    )


def serve_source_review(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    server = create_source_review_server(
        project=project, workspace_root=workspace_root, port=port
    )
    print(f"Source review server: {server.review_url}")
    if open_browser:
        webbrowser.open(server.review_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
