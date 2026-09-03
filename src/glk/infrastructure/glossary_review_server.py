"""Localhost-only HTTP server for browser-based glossary review."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from importlib import resources
import json
from pathlib import Path
from urllib.parse import urlsplit
import webbrowser

from glk.application.glossary_ai_service import (
    GlossaryAiTriageError,
    GlossaryTriageProvider,
    estimate_project_glossary_ai_triage,
    get_project_glossary_ai_suggestions,
    triage_project_glossary_candidates,
)
from glk.application.glossary_review_service import (
    GlossaryReviewConflictError,
    GlossaryReviewError,
    get_project_glossary_review_document,
    save_project_glossary_review,
)
from glk.application.glossary_service import (
    GlossaryImportError,
    GlossaryManualTermsMissingError,
    import_project_glossary,
)
from glk.error_response import (
    localized_detail_message,
    make_error_response,
)
from glk.infrastructure.local_http import (
    LocalHttpRequestHandler,
    LocalHttpServer,
    validate_local_port,
    validate_local_return_url,
)


_MAX_REQUEST_BYTES = 8 * 1024 * 1024


class GlossaryReviewHttpServer(LocalHttpServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        project: str | Path,
        workspace_root: str | Path,
        settings_root: str | Path | None = None,
        return_url: str | None = None,
        glossary_ai_provider: GlossaryTriageProvider | None = None,
    ) -> None:
        self.return_url = validate_local_return_url(
            return_url,
            label="Glossary review",
        )
        super().__init__(server_address, handler_class)
        self.project = str(project)
        self.workspace_root = str(workspace_root)
        self.settings_root = settings_root
        self.glossary_ai_provider = glossary_ai_provider

    @property
    def review_url(self) -> str:
        return self.root_url


class _GlossaryReviewHandler(LocalHttpRequestHandler):
    server: GlossaryReviewHttpServer
    request_error_type = GlossaryReviewError

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
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
        if self._send_web_asset(path):
            return
        if path == "/":
            template = (
                resources.files("glk.web")
                .joinpath("glossary_review.html")
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
                document = get_project_glossary_review_document(
                    project=self.server.project,
                    workspace_root=self.server.workspace_root,
                )
            except GlossaryReviewConflictError as error:
                self._send_error_json(
                    HTTPStatus.CONFLICT,
                    error,
                    code=error.code,
                )
                return
            except GlossaryReviewError as error:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    error,
                    code=error.code,
                )
                return
            except (OSError, ValueError) as error:
                self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    error,
                    code="INTERNAL_ERROR",
                )
                return
            self._send_json(HTTPStatus.OK, document)
            return
        if path == "/api/ai-triage":
            if not self._api_authorized():
                self._send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "Invalid review session.",
                    code="REVIEW_SESSION_INVALID",
                )
                return
            try:
                result = get_project_glossary_ai_suggestions(
                    project=self.server.project,
                    workspace_root=self.server.workspace_root,
                    settings_root=self.server.settings_root,
                    provider=self.server.glossary_ai_provider,
                )
            except GlossaryReviewConflictError as error:
                self._send_error_json(
                    HTTPStatus.CONFLICT,
                    error,
                    code=error.code,
                )
                return
            except (GlossaryReviewError, OSError, ValueError) as error:
                code = getattr(error, "code", "INVALID_REQUEST")
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    error,
                    code=code,
                    message=localized_detail_message(error),
                )
                return
            self._send_json(HTTPStatus.OK, {"ok": True, **result.to_dict()})
            return
        self._send_error_json(
            HTTPStatus.NOT_FOUND,
            "Not found.",
            code="RESOURCE_NOT_FOUND",
        )

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {
            "/api/save",
            "/api/import",
            "/api/ai-triage/estimate",
            "/api/ai-triage",
        }:
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
            rows = body.get("rows")
            if not isinstance(review_hash, str):
                raise GlossaryReviewError("review_sha256 is required.")
            if not isinstance(rows, list):
                raise GlossaryReviewError("rows must be a list.")
            with self.server.mutation_lock:
                if path == "/api/ai-triage/estimate":
                    estimate = estimate_project_glossary_ai_triage(
                        project=self.server.project,
                        workspace_root=self.server.workspace_root,
                        settings_root=self.server.settings_root,
                        expected_review_sha256=review_hash,
                        rows=rows,
                        provider=self.server.glossary_ai_provider,
                    )
                    response = {"ok": True, **estimate.to_dict()}
                elif path == "/api/ai-triage":
                    triage_result = triage_project_glossary_candidates(
                        project=self.server.project,
                        workspace_root=self.server.workspace_root,
                        settings_root=self.server.settings_root,
                        expected_review_sha256=review_hash,
                        rows=rows,
                        provider=self.server.glossary_ai_provider,
                    )
                    response = {"ok": True, **triage_result.to_dict()}
                else:
                    document = save_project_glossary_review(
                        project=self.server.project,
                        workspace_root=self.server.workspace_root,
                        rows=rows,
                        expected_review_sha256=review_hash,
                    )
                    if path == "/api/save":
                        response = {
                            "ok": True,
                            "document": document,
                        }
                    else:
                        try:
                            import_result = import_project_glossary(
                                project=self.server.project,
                                workspace_root=self.server.workspace_root,
                                file="03_terminology/glossary_review.tsv",
                                allow_missing_terms=bool(
                                    body.get("allow_missing_terms", False)
                                ),
                            )
                        except GlossaryManualTermsMissingError as error:
                            response = dict(
                                make_error_response(
                                    error.code,
                                    error,
                                    message=localized_detail_message(error),
                                ).to_dict()
                            )
                            response["missing_terms"] = list(error.source_terms)
                            response["document"] = document
                        except GlossaryImportError as error:
                            response = dict(
                                make_error_response(
                                    "GLOSSARY_IMPORT_FAILED",
                                    error,
                                    message=localized_detail_message(error),
                                ).to_dict()
                            )
                            response["document"] = document
                        else:
                            response = {
                                "ok": True,
                                "result": import_result.to_dict(),
                                "document": get_project_glossary_review_document(
                                    project=self.server.project,
                                    workspace_root=self.server.workspace_root,
                                ),
                            }
        except (GlossaryAiTriageError, GlossaryReviewError, OSError, ValueError) as error:
            status = (
                HTTPStatus.CONFLICT
                if isinstance(error, GlossaryReviewConflictError)
                else HTTPStatus.BAD_REQUEST
            )
            code = (
                error.code
                if isinstance(error, GlossaryReviewError)
                else "INVALID_REQUEST"
            )
            self._send_error_json(status, error, code=code)
            return
        self._send_json(HTTPStatus.OK, response)


def create_glossary_review_server(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    settings_root: str | Path | None = None,
    port: int = 0,
    return_url: str | None = None,
    glossary_ai_provider: GlossaryTriageProvider | None = None,
) -> GlossaryReviewHttpServer:
    validate_local_port(port, error_type=GlossaryReviewError)
    get_project_glossary_review_document(
        project=project,
        workspace_root=workspace_root,
    )
    return GlossaryReviewHttpServer(
        ("127.0.0.1", port),
        _GlossaryReviewHandler,
        project=project,
        workspace_root=workspace_root,
        settings_root=settings_root,
        return_url=return_url,
        glossary_ai_provider=glossary_ai_provider,
    )


def serve_glossary_review(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    server = create_glossary_review_server(
        project=project,
        workspace_root=workspace_root,
        port=port,
    )
    print(f"Glossary review: {server.review_url}")
    print("Press Ctrl+C to stop the local review server.")
    if open_browser:
        webbrowser.open(server.review_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
