"""Localhost-only HTTP server for browser-based translation review."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from importlib import resources
import json
from pathlib import Path
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
from glk.application.translation_retry_job_service import (
    TranslationRetryJobConflict,
    TranslationRetryJobError,
    TranslationRetryJobManager,
    TranslationRetryJobRunner,
)
from glk.application.translation_types import TranslationError
from glk.infrastructure.gemini_common import GeminiConfigurationError
from glk.infrastructure.local_http import (
    LocalHttpRequestHandler,
    LocalHttpServer,
    validate_local_port,
    validate_local_return_url,
)


_MAX_REQUEST_BYTES = 8 * 1024 * 1024


class TranslationReviewHttpServer(LocalHttpServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        project: str | Path,
        workspace_root: str | Path,
        settings_root: str | Path | None = None,
        return_url: str | None = None,
        retry_runner: TranslationRetryJobRunner | None = None,
    ) -> None:
        self.return_url = validate_local_return_url(
            return_url,
            label="Translation review",
        )
        super().__init__(server_address, handler_class)
        self.project = str(project)
        self.workspace_root = str(workspace_root)
        self.retry_jobs = TranslationRetryJobManager(
            project=project,
            workspace_root=workspace_root,
            settings_root=settings_root,
            runner=retry_runner,
        )

    @property
    def review_url(self) -> str:
        return self.root_url

    def server_close(self) -> None:
        self.retry_jobs.close()
        super().server_close()


class _TranslationReviewHandler(LocalHttpRequestHandler):
    server: TranslationReviewHttpServer
    request_error_type = TranslationReviewError

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
            ).replace(
                "__GLK_RETURN_URL_JSON__",
                json.dumps(self.server.return_url),
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
        if path == "/api/retry-job":
            if not self._api_authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "Invalid review session.")
                return
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "job": self.server.retry_jobs.get_job()},
            )
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/save", "/api/qa", "/api/retry", "/api/finalize"}:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if not self._api_authorized():
            self._send_error_json(HTTPStatus.FORBIDDEN, "Invalid review session.")
            return
        try:
            body = self._read_request_json(max_bytes=_MAX_REQUEST_BYTES)
            review_hash = body.get("review_sha256")
            translations = body.get("translations")
            if not isinstance(review_hash, str):
                raise TranslationReviewError("review_sha256 is required.")
            if not isinstance(translations, dict):
                raise TranslationReviewError("translations must be an object.")
            with self.server.mutation_lock:
                if self.server.retry_jobs.is_active():
                    message = (
                        "오류 문장 재번역이 이미 진행 중입니다."
                        if path == "/api/retry"
                        else (
                            "오류 문장 재번역 중에는 검수 내용을 "
                            "변경할 수 없습니다."
                        )
                    )
                    raise TranslationRetryJobConflict(message)
                document = save_project_translation_review(
                    project=self.server.project,
                    workspace_root=self.server.workspace_root,
                    translations=translations,
                    expected_review_sha256=review_hash,
                )
                if path == "/api/save":
                    response: dict[str, Any] = {"ok": True, "document": document}
                elif path == "/api/qa":
                    qa_result = run_project_translation_qa(
                        project=self.server.project,
                        workspace_root=self.server.workspace_root,
                    )
                    response = {
                        "ok": qa_result.passed,
                        "result": qa_result.to_dict(),
                        "document": get_project_translation_review_document(
                            project=self.server.project,
                            workspace_root=self.server.workspace_root,
                        ),
                    }
                elif path == "/api/retry":
                    job = self.server.retry_jobs.start(
                        expected_review_sha256=document["review_sha256"],
                    )
                    response = {
                        "ok": True,
                        "job": job,
                        "document": document,
                    }
                else:
                    finalize_result = finalize_project_translation_review(
                        project=self.server.project,
                        workspace_root=self.server.workspace_root,
                    )
                    response = {
                        "ok": finalize_result.valid,
                        "result": finalize_result.to_dict(),
                        "document": get_project_translation_review_document(
                            project=self.server.project,
                            workspace_root=self.server.workspace_root,
                        ),
                    }
        except (
            TranslationError,
            TranslationReviewError,
            TranslationRetryJobConflict,
            TranslationRetryJobError,
            GeminiConfigurationError,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            status = (
                HTTPStatus.CONFLICT
                if isinstance(error, TranslationRetryJobConflict)
                or "changed after this page was loaded" in str(error)
                else HTTPStatus.BAD_REQUEST
            )
            self._send_error_json(status, str(error))
            return
        self._send_json(
            HTTPStatus.ACCEPTED if path == "/api/retry" else HTTPStatus.OK,
            response,
        )


def create_translation_review_server(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    settings_root: str | Path | None = None,
    port: int = 0,
    return_url: str | None = None,
    retry_runner: TranslationRetryJobRunner | None = None,
) -> TranslationReviewHttpServer:
    validate_local_port(port, error_type=TranslationReviewError)
    get_project_translation_review_document(
        project=project,
        workspace_root=workspace_root,
    )
    return TranslationReviewHttpServer(
        ("127.0.0.1", port),
        _TranslationReviewHandler,
        project=project,
        workspace_root=workspace_root,
        settings_root=settings_root,
        return_url=return_url,
        retry_runner=retry_runner,
    )


def serve_translation_review(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    settings_root: str | Path | None = None,
    port: int = 0,
    open_browser: bool = True,
) -> None:
    server = create_translation_review_server(
        project=project,
        workspace_root=workspace_root,
        settings_root=settings_root,
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
