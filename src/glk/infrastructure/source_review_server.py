"""Localhost-only HTTP server for visual source review."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from importlib import resources
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
import webbrowser

from glk.application.project_service import load_project
from glk.application.review_types import SourceReviewDocument
from glk.application.source_review_service import (
    SourceReviewConflictError,
    SourceReviewError,
    finalize_project_source_review,
    get_project_source_review_document,
    save_project_source_review,
)
from glk.domain.workspace import WorkspacePaths, is_pdf_source_file
from glk.infrastructure.local_http import (
    LocalHttpRequestHandler,
    LocalHttpServer,
    local_security_headers,
    validate_local_port,
    validate_local_return_url,
)


_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_SOURCE_SECURITY_HEADERS = local_security_headers(allow_blob_images=True)


class SourceReviewHttpServer(LocalHttpServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        project: str | Path,
        workspace_root: str | Path,
        return_url: str | None = None,
    ) -> None:
        self.return_url = validate_local_return_url(
            return_url,
            label="Source review",
        )
        super().__init__(server_address, handler_class)
        self.project = str(project)
        self.workspace_root = str(workspace_root)

    @property
    def review_url(self) -> str:
        return self.root_url


class _SourceReviewHandler(LocalHttpRequestHandler):
    server: SourceReviewHttpServer
    request_error_type = SourceReviewError
    security_headers = _SOURCE_SECURITY_HEADERS

    def _asset_authorized(self, query: dict[str, list[str]]) -> bool:
        supplied_token = query.get("token", [""])[0]
        return (
            self._host_is_local()
            and self._origin_allowed()
            and self._token_matches(supplied_token)
        )

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.is_file():
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                "Source asset not found.",
                code="RESOURCE_NOT_FOUND",
            )
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
                self._send_bytes(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    b"",
                    "application/octet-stream",
                    extra_headers={"Content-Range": f"bytes */{file_size}"},
                )
                return
        length = end - start + 1
        guessed = content_type or mimetypes.guess_type(path.name)[0]
        self.send_response(status)
        extra_headers = {"Accept-Ranges": "bytes"}
        if status == HTTPStatus.PARTIAL_CONTENT:
            extra_headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        self._send_standard_headers(
            guessed or "application/octet-stream",
            length,
            extra_headers=extra_headers,
        )
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

    def _document(self) -> SourceReviewDocument:
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
            page = group["page"]
            if page is None:
                raise SourceReviewError("PDF review group has no page number.")
            return paths.pdf_pages / f"page_{page:03d}.png"
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
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Only localhost is allowed.",
                code="LOCAL_ACCESS_REQUIRED",
            )
            return
        if path == "/":
            template = (
                resources.files("glk.web")
                .joinpath("source_review.html")
                .read_text(encoding="utf-8")
            )
            html = template.replace(
                "__GLK_TOKEN_JSON__", json.dumps(self.server.auth_token)
            ).replace(
                "__GLK_RETURN_URL_JSON__", json.dumps(self.server.return_url)
            ).encode("utf-8")
            self._send_bytes(HTTPStatus.OK, html, "text/html; charset=utf-8")
            return
        if path == "/api/review":
            if not self._api_authorized():
                self._send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "Invalid review session.",
                    code="REVIEW_SESSION_INVALID",
                )
                return
            try:
                self._send_json(HTTPStatus.OK, self._document())
            except SourceReviewConflictError as error:
                self._send_error_json(
                    HTTPStatus.CONFLICT,
                    error,
                    code=error.code,
                )
            except SourceReviewError as error:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    error,
                    code=error.code,
                )
            except (OSError, ValueError) as error:
                self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    error,
                    code="INTERNAL_ERROR",
                )
            return
        if path == "/api/source-image":
            if not self._asset_authorized(query):
                self._send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "Invalid review session.",
                    code="REVIEW_SESSION_INVALID",
                )
                return
            try:
                self._send_file(self._group_asset(query.get("group", [""])[0]))
            except (SourceReviewError, OSError, ValueError) as error:
                self._send_error_json(
                    HTTPStatus.NOT_FOUND,
                    error,
                    code="RESOURCE_NOT_FOUND",
                )
            return
        if path == "/api/original-pdf":
            if not self._asset_authorized(query):
                self._send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "Invalid review session.",
                    code="REVIEW_SESSION_INVALID",
                )
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
                self._send_error_json(
                    HTTPStatus.NOT_FOUND,
                    error,
                    code="RESOURCE_NOT_FOUND",
                )
            return
        self._send_error_json(
            HTTPStatus.NOT_FOUND,
            "Not found.",
            code="RESOURCE_NOT_FOUND",
        )

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/save", "/api/validate", "/api/finalize"}:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                "Not found.",
                code="RESOURCE_NOT_FOUND",
            )
            return
        if not self._api_authorized():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Invalid review session.",
                code="REVIEW_SESSION_INVALID",
            )
            return
        try:
            body = self._read_request_json(max_bytes=_MAX_REQUEST_BYTES)
            review_hash = body.get("review_sha256")
            blocks = body.get("blocks")
            allow_token_changes = body.get("allow_token_changes", False)
            allow_unresolved_icons = body.get("allow_unresolved_icons", False)
            if not isinstance(review_hash, str):
                raise SourceReviewError("review_sha256 is required.")
            if not isinstance(blocks, list):
                raise SourceReviewError("blocks must be a list.")
            if not isinstance(allow_token_changes, bool):
                raise SourceReviewError("allow_token_changes must be true or false.")
            if not isinstance(allow_unresolved_icons, bool):
                raise SourceReviewError(
                    "allow_unresolved_icons must be true or false."
                )
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
                        allow_unresolved_icons=allow_unresolved_icons,
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
                if isinstance(error, SourceReviewConflictError)
                else HTTPStatus.BAD_REQUEST
            )
            self._send_error_json(status, error, code=error.code)
        except (OSError, ValueError) as error:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                error,
                code="INTERNAL_ERROR",
            )


def create_source_review_server(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    port: int = 0,
    return_url: str | None = None,
) -> SourceReviewHttpServer:
    validate_local_port(port, error_type=SourceReviewError)
    get_project_source_review_document(
        project=project, workspace_root=workspace_root
    )
    return SourceReviewHttpServer(
        ("127.0.0.1", port),
        _SourceReviewHandler,
        project=project,
        workspace_root=workspace_root,
        return_url=return_url,
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
