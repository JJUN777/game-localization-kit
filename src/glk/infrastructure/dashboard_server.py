"""Localhost-only HTTP server for the project dashboard."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import json
from pathlib import Path
import re
import secrets
from send2trash import send2trash
from socketserver import TCPServer
import tempfile
import threading
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit
import webbrowser

from glk.application._io import write_bytes_atomic
from glk.application.ai_model_catalog import (
    GeminiModelCatalogError,
    load_gemini_model_catalog,
)
from glk.application.ai_settings_service import (
    AiSettingsError,
    AiSettingsService,
)
from glk.application.dashboard_job_service import (
    DashboardJobConflict,
    DashboardJobError,
    DashboardJobManager,
    GlossaryJobRunner,
    SourceJobRunner,
    TranslationJobRunner,
)
from glk.application.dashboard_service import (
    DashboardOutputError,
    get_dashboard_document,
    get_project_dashboard_output,
)
from glk.application.project_service import (
    ProjectNotFoundError,
    create_project as create_project_workspace,
    load_workspace_project_id,
)
from glk.application.source_registration_service import (
    MAX_OCR_PROMPT_BYTES,
    SUPPORTED_IMAGE_EXTENSIONS,
    SourceRecoveryError,
    SourceRegistrationError,
    project_has_source_files,
    register_image_sources,
    register_pdf_source,
    replace_image_sources,
    replace_pdf_source,
    save_project_ocr_prompt,
)
from glk.application.translation_prompt_service import (
    MAX_TRANSLATION_PROMPT_BYTES,
    TranslationPromptError,
    save_project_translation_prompt,
)
from glk.error_response import make_http_error_response
from glk.infrastructure.glossary_review_server import (
    create_glossary_review_server,
)
from glk.infrastructure.source_review_server import create_source_review_server
from glk.infrastructure.translation_review_server import (
    create_translation_review_server,
)


_MAX_REQUEST_BYTES = 128 * 1024
_MAX_OCR_PROMPT_REQUEST_BYTES = MAX_OCR_PROMPT_BYTES * 6 + 1024
_MAX_TRANSLATION_PROMPT_REQUEST_BYTES = (
    MAX_TRANSLATION_PROMPT_BYTES * 6 + 1024
)
_MAX_UPLOAD_BYTES = 256 * 1024 * 1024
_MAX_UPLOAD_FILES = 200
_REVIEW_TYPES = {"source", "glossary", "translation"}
_UNSAFE_UPLOAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
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
        settings_root: str | Path,
        source_job_runner: SourceJobRunner | None = None,
        glossary_job_runner: GlossaryJobRunner | None = None,
        translation_job_runner: TranslationJobRunner | None = None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.workspace_root = str(workspace_root)
        self.ai_settings = AiSettingsService(settings_root)
        self.job_manager = DashboardJobManager(
            workspace_root,
            runner=source_job_runner,
            glossary_runner=glossary_job_runner,
            translation_runner=translation_job_runner,
        )
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
        location = load_workspace_project_id(project_id, self.workspace_root)

        key = (project_id, review_type)
        with self._review_lock:
            existing = self._review_servers.get(key)
            if existing is not None and existing[1].is_alive():
                return str(getattr(existing[0], "review_url"))
            if existing is not None:
                existing[0].server_close()
                self._review_servers.pop(key, None)

            review_server: ThreadingHTTPServer
            if review_type == "source":
                review_server = create_source_review_server(
                    project=location.path,
                    workspace_root=self.workspace_root,
                    port=0,
                    return_url=self.dashboard_url,
                )
            elif review_type == "glossary":
                review_server = create_glossary_review_server(
                    project=location.path,
                    workspace_root=self.workspace_root,
                    port=0,
                    return_url=self.dashboard_url,
                )
            else:
                review_server = create_translation_review_server(
                    project=location.path,
                    workspace_root=self.workspace_root,
                    port=0,
                    return_url=self.dashboard_url,
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
        self.job_manager.close()
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
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in (extra_headers or {}).items():
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
        max_bytes: int = _MAX_REQUEST_BYTES,
    ) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("application/json"):
            raise DashboardError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise DashboardError("Invalid Content-Length.") from error
        if length <= 0 or length > max_bytes:
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

    def _read_source_upload(
        self,
    ) -> tuple[str, list[tuple[str, bytes]], str | None]:
        content_type = self.headers.get("Content-Type", "")
        if (
            "\r" in content_type
            or "\n" in content_type
            or not content_type.casefold().startswith("multipart/form-data")
        ):
            raise DashboardError(
                "Content-Type must be multipart/form-data."
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise DashboardError("Invalid Content-Length.") from error
        if length <= 0 or length > _MAX_UPLOAD_BYTES:
            raise DashboardError(
                "Upload body size must be between 1 byte and 256 MiB."
            )

        envelope = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
        ).encode("ascii") + self.rfile.read(length)
        message = BytesParser(policy=policy.default).parsebytes(envelope)
        if not message.is_multipart():
            raise DashboardError("Upload body must be valid multipart data.")

        source_types: list[str] = []
        ocr_prompts: list[str] = []
        files: list[tuple[str, bytes]] = []
        seen_names: set[str] = set()
        for part in message.walk():
            if part.is_multipart():
                continue
            if part.get_content_disposition() != "form-data":
                raise DashboardError("Upload part must use form-data.")
            field_name = part.get_param(
                "name",
                header="content-disposition",
            )
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                raise DashboardError("Upload part has invalid content.")
            filename = part.get_filename()
            if field_name in {"source_type", "ocr_prompt"} and filename is None:
                try:
                    text = payload.decode(
                        part.get_content_charset() or "utf-8"
                    )
                except (LookupError, UnicodeDecodeError) as error:
                    raise DashboardError(
                        f"{field_name} must be UTF-8 text."
                    ) from error
                if field_name == "source_type":
                    source_types.append(text.strip())
                else:
                    ocr_prompts.append(text)
                continue
            if field_name != "files" or filename is None:
                raise DashboardError("Upload contains an unknown form field.")

            safe_name = filename.strip()
            stem = Path(safe_name).stem.upper()
            if (
                not safe_name
                or len(safe_name) > 240
                or safe_name in {".", ".."}
                or safe_name.endswith((".", " "))
                or _UNSAFE_UPLOAD_NAME.search(safe_name)
                or stem in _WINDOWS_RESERVED_NAMES
            ):
                raise DashboardError(
                    f"Upload filename is not portable: {filename}"
                )
            name_key = safe_name.casefold()
            if name_key in seen_names:
                raise DashboardError(
                    f"Upload contains a duplicate filename: {safe_name}"
                )
            if not payload:
                raise DashboardError(
                    f"Uploaded file is empty: {safe_name}"
                )
            seen_names.add(name_key)
            files.append((safe_name, payload))
            if len(files) > _MAX_UPLOAD_FILES:
                raise DashboardError(
                    f"Upload supports at most {_MAX_UPLOAD_FILES} files."
                )

        if len(source_types) != 1 or source_types[0] not in {"pdf", "images"}:
            raise DashboardError("source_type must be pdf or images.")
        if not files:
            raise DashboardError("Select at least one source file.")
        source_type = source_types[0]
        if source_type == "pdf":
            if ocr_prompts:
                raise DashboardError(
                    "OCR prompt is available only for image sources."
                )
            if len(files) != 1 or Path(files[0][0]).suffix.casefold() != ".pdf":
                raise DashboardError("Select exactly one PDF file.")
            if b"%PDF-" not in files[0][1][:1024]:
                raise DashboardError("The selected file is not a valid PDF.")
        else:
            unsupported = [
                name
                for name, _ in files
                if Path(name).suffix.casefold()
                not in SUPPORTED_IMAGE_EXTENSIONS
            ]
            if unsupported:
                raise DashboardError(
                    "Unsupported image file: " + ", ".join(unsupported[:3])
                )
        if len(ocr_prompts) > 1:
            raise DashboardError("Upload must contain at most one OCR prompt.")
        return source_type, files, ocr_prompts[0] if ocr_prompts else None

    def _register_uploaded_source(
        self,
        project_id: str,
        source_type: str,
        uploads: list[tuple[str, bytes]],
        ocr_prompt: str | None,
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        location = load_workspace_project_id(
            project_id,
            self.server.workspace_root,
        )
        if not replace and (
            location.manifest.source_file is not None
            or project_has_source_files(location)
        ):
            raise SourceRegistrationError(
                "Project already has source files. Use source replacement "
                "before extraction or OCR starts."
            )

        with tempfile.TemporaryDirectory(prefix="glk-upload-") as temporary:
            upload_root = Path(temporary)
            upload_paths: list[Path] = []
            registered_files: tuple[Path, ...]
            for filename, content in uploads:
                upload_path = upload_root / filename
                write_bytes_atomic(upload_path, content)
                upload_paths.append(upload_path)

            if source_type == "pdf":
                registered_pdf = (
                    replace_pdf_source(location, upload_paths[0])
                    if replace
                    else register_pdf_source(location, upload_paths[0])
                )
                registered_location = registered_pdf.location
                registered_files = (registered_pdf.path,)
            else:
                registered_images = (
                    replace_image_sources(
                        location,
                        upload_root,
                        upload_paths,
                        ocr_prompt=ocr_prompt,
                    )
                    if replace
                    else register_image_sources(
                        location,
                        upload_root,
                        upload_paths,
                        ocr_prompt=ocr_prompt,
                    )
                )
                registered_location = registered_images.location
                registered_files = registered_images.files

        return {
            "replaced": replace,
            "source_type": source_type,
            "source_file": registered_location.manifest.source_file,
            "ocr_prompt_updated": (
                source_type == "images" and ocr_prompt is not None
            ),
            "files": [
                path.relative_to(registered_location.path).as_posix()
                for path in registered_files
            ],
        }

    @staticmethod
    def _source_upload_project_id(path: str) -> str | None:
        prefix = "/api/projects/"
        suffix = "/source"
        if (
            not path.startswith(prefix)
            or not path.endswith(suffix)
            or path == prefix + suffix.lstrip("/")
        ):
            return None
        return unquote(path[len(prefix) : -len(suffix)])

    @staticmethod
    def _ocr_prompt_project_id(path: str) -> str | None:
        prefix = "/api/projects/"
        suffix = "/ocr-prompt"
        if (
            not path.startswith(prefix)
            or not path.endswith(suffix)
            or path == prefix + suffix.lstrip("/")
        ):
            return None
        return unquote(path[len(prefix) : -len(suffix)])

    @staticmethod
    def _translation_prompt_project_id(path: str) -> str | None:
        prefix = "/api/projects/"
        suffix = "/translation-prompt"
        if (
            not path.startswith(prefix)
            or not path.endswith(suffix)
            or path == prefix + suffix.lstrip("/")
        ):
            return None
        return unquote(path[len(prefix) : -len(suffix)])

    def _handle_source_upload(
        self,
        path: str,
        *,
        replace: bool,
    ) -> None:
        upload_project_id = self._source_upload_project_id(path)
        error_code = (
            "SOURCE_REPLACE_FAILED"
            if replace
            else "SOURCE_REGISTER_FAILED"
        )
        if upload_project_id is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if not self._api_authorized():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Invalid review session.",
            )
            return
        if (
            not upload_project_id
            or "/" in upload_project_id
            or "\\" in upload_project_id
        ):
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "Project ID must not contain path separators.",
                code=error_code,
            )
            return
        if self.server.job_manager.is_project_active(upload_project_id):
            self._send_error_json(
                HTTPStatus.CONFLICT,
                "Source replacement is unavailable while a source job is running.",
                code="SOURCE_JOB_CONFLICT",
            )
            return
        try:
            source_type, uploads, ocr_prompt = self._read_source_upload()
            with self.server.mutation_lock:
                if self.server.job_manager.is_project_active(
                    upload_project_id
                ):
                    self._send_error_json(
                        HTTPStatus.CONFLICT,
                        (
                            "Source replacement is unavailable while "
                            "a source job is running."
                        ),
                        code="SOURCE_JOB_CONFLICT",
                    )
                    return
                source = self._register_uploaded_source(
                    upload_project_id,
                    source_type,
                    uploads,
                    ocr_prompt,
                    replace=replace,
                )
        except ProjectNotFoundError as error:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                error,
                code="RESOURCE_NOT_FOUND",
            )
            return
        except SourceRecoveryError as error:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                error,
                code="SOURCE_REPLACE_FAILED",
                message=str(error),
            )
            return
        except (
            DashboardError,
            SourceRegistrationError,
            TypeError,
            ValueError,
        ) as error:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                error,
                code=error_code,
            )
            return
        except (OSError, RuntimeError) as error:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                error,
                code=error_code,
            )
            return
        self._send_json(
            HTTPStatus.OK if replace else HTTPStatus.CREATED,
            {"ok": True, "source": source},
        )

    def do_GET(self) -> None:
        parsed_url = urlsplit(self.path)
        path = parsed_url.path
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
        if path == "/api/jobs":
            if not self._api_authorized():
                self._send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "Invalid review session.",
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "jobs": self.server.job_manager.list_jobs(),
                    "glossary_jobs": (
                        self.server.job_manager.list_glossary_jobs()
                    ),
                    "translation_jobs": (
                        self.server.job_manager.list_translation_jobs()
                    ),
                },
            )
            return
        if path == "/api/settings/ai":
            if not self._api_authorized():
                self._send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "Invalid review session.",
                )
                return
            try:
                settings = self.server.ai_settings.status()
                model_catalog = load_gemini_model_catalog()
            except (
                AiSettingsError,
                GeminiModelCatalogError,
            ) as error:
                self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    error,
                    code="AI_SETTINGS_LOAD_FAILED",
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "settings": settings.to_dict(),
                    "model_catalog": model_catalog,
                },
            )
            return
        if path == "/api/output":
            if not self._api_authorized():
                self._send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "Invalid review session.",
                )
                return
            try:
                query = parse_qs(
                    parsed_url.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
                if (
                    set(query) != {"project_id", "path"}
                    or len(query["project_id"]) != 1
                    or len(query["path"]) != 1
                ):
                    raise DashboardOutputError(
                        "프로젝트와 결과 파일을 정확히 하나씩 선택하세요."
                    )
                output = get_project_dashboard_output(
                    project_id=query["project_id"][0],
                    output_path=query["path"][0],
                    workspace_root=self.server.workspace_root,
                )
                data = output.path.read_bytes()
                if hashlib.sha256(data).hexdigest() != output.sha256:
                    raise DashboardOutputError(
                        "최종 번역 파일이 승인 이후 변경되었습니다."
                    )
            except (DashboardOutputError, OSError, ValueError) as error:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    error,
                    code="OUTPUT_DOWNLOAD_FAILED",
                )
                return
            encoded_name = quote(output.download_name, safe="")
            self._send_bytes(
                HTTPStatus.OK,
                data,
                "application/octet-stream",
                extra_headers={
                    "Content-Disposition": (
                        "attachment; filename=\"translation.txt\"; "
                        f"filename*=UTF-8''{encoded_name}"
                    ),
                },
            )
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        is_source_upload = self._source_upload_project_id(path) is not None
        if (
            path not in {
                "/api/projects",
                "/api/review/open",
                "/api/jobs/source",
                "/api/jobs/glossary",
                "/api/jobs/translation",
            }
            and not is_source_upload
        ):
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if not self._api_authorized():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Invalid review session.",
            )
            return
        if is_source_upload:
            self._handle_source_upload(path, replace=False)
            return
        try:
            request = self._read_request_json()
            if path == "/api/jobs/source":
                project_id = request.get("project_id")
                if not isinstance(project_id, str) or not project_id.strip():
                    raise DashboardJobError("project_id is required.")
                if "/" in project_id or "\\" in project_id:
                    raise DashboardJobError(
                        "Project ID must not contain path separators."
                    )
                settings = self.server.ai_settings.status()
                if not settings.api_key_configured:
                    raise DashboardJobError(
                        "GEMINI_API_KEY is not configured."
                    )
                with self.server.mutation_lock:
                    job = self.server.job_manager.start_source_job(
                        project_id=project_id.strip(),
                        model=settings.model,
                    )
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {"ok": True, "job": job},
                )
                return
            if path == "/api/jobs/glossary":
                project_id = request.get("project_id")
                if not isinstance(project_id, str) or not project_id.strip():
                    raise DashboardJobError("project_id is required.")
                if "/" in project_id or "\\" in project_id:
                    raise DashboardJobError(
                        "Project ID must not contain path separators."
                    )
                with self.server.mutation_lock:
                    job = self.server.job_manager.start_glossary_job(
                        project_id=project_id.strip(),
                    )
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {"ok": True, "job": job},
                )
                return
            if path == "/api/jobs/translation":
                project_id = request.get("project_id")
                prompt = request.get("prompt")
                force = request.get("force", False)
                if not isinstance(project_id, str) or not project_id.strip():
                    raise DashboardJobError("project_id is required.")
                if "/" in project_id or "\\" in project_id:
                    raise DashboardJobError(
                        "Project ID must not contain path separators."
                    )
                if not isinstance(prompt, str):
                    raise DashboardJobError("prompt must be a string.")
                if not isinstance(force, bool):
                    raise DashboardJobError("force must be a boolean.")
                settings = self.server.ai_settings.status()
                if not settings.api_key_configured:
                    raise DashboardJobError(
                        "GEMINI_API_KEY is not configured."
                    )
                with self.server.mutation_lock:
                    job = self.server.job_manager.start_translation_job(
                        project_id=project_id.strip(),
                        model=settings.model,
                        prompt=prompt,
                        force=force,
                    )
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {"ok": True, "job": job},
                )
                return
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
        except DashboardJobConflict as error:
            conflict_code = (
                "GLOSSARY_JOB_CONFLICT"
                if path == "/api/jobs/glossary"
                else "TRANSLATION_JOB_CONFLICT"
                if path == "/api/jobs/translation"
                else "SOURCE_JOB_CONFLICT"
            )
            self._send_error_json(
                HTTPStatus.CONFLICT,
                error,
                code=conflict_code,
            )
            return
        except (
            AiSettingsError,
            DashboardError,
            DashboardJobError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            code = (
                "PROJECT_INIT_FAILED"
                if path == "/api/projects"
                else "SOURCE_JOB_START_FAILED"
                if path == "/api/jobs/source"
                else "GLOSSARY_JOB_START_FAILED"
                if path == "/api/jobs/glossary"
                else "TRANSLATION_JOB_START_FAILED"
                if path == "/api/jobs/translation"
                else "INVALID_REQUEST"
            )
            self._send_error_json(HTTPStatus.BAD_REQUEST, error, code=code)
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "url": url})

    def do_PUT(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/settings/ai":
            if not self._api_authorized():
                self._send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "Invalid review session.",
                )
                return
            try:
                request = self._read_request_json()
                api_key = request.get("api_key")
                model = request.get("model")
                if api_key is not None and not isinstance(api_key, str):
                    raise DashboardError(
                        "api_key must be a string or null."
                    )
                if not isinstance(model, str):
                    raise DashboardError("model must be a string.")
                with self.server.mutation_lock:
                    settings = self.server.ai_settings.save(
                        api_key=api_key,
                        model=model,
                    )
            except (
                AiSettingsError,
                DashboardError,
                TypeError,
                ValueError,
            ) as error:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    error,
                    code="AI_SETTINGS_UPDATE_FAILED",
                )
                return
            except (OSError, RuntimeError) as error:
                self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    error,
                    code="AI_SETTINGS_UPDATE_FAILED",
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "settings": settings.to_dict()},
            )
            return
        self._handle_source_upload(path, replace=True)

    def do_PATCH(self) -> None:
        path = urlsplit(self.path).path
        prompt_kind = "ocr"
        project_id = self._ocr_prompt_project_id(path)
        if project_id is None:
            prompt_kind = "translation"
            project_id = self._translation_prompt_project_id(path)
        if project_id is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if not self._api_authorized():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Invalid review session.",
            )
            return
        if not project_id or "/" in project_id or "\\" in project_id:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "Project ID must not contain path separators.",
                code=(
                    "OCR_PROMPT_UPDATE_FAILED"
                    if prompt_kind == "ocr"
                    else "TRANSLATION_PROMPT_UPDATE_FAILED"
                ),
            )
            return
        if self.server.job_manager.is_project_active(project_id):
            self._send_error_json(
                HTTPStatus.CONFLICT,
                (
                    "OCR prompt editing is unavailable while a source job is running."
                    if prompt_kind == "ocr"
                    else "번역 프롬프트는 백그라운드 작업 중에 수정할 수 없습니다."
                ),
                code=(
                    "SOURCE_JOB_CONFLICT"
                    if prompt_kind == "ocr"
                    else "TRANSLATION_JOB_CONFLICT"
                ),
            )
            return
        try:
            request = self._read_request_json(
                max_bytes=(
                    _MAX_OCR_PROMPT_REQUEST_BYTES
                    if prompt_kind == "ocr"
                    else _MAX_TRANSLATION_PROMPT_REQUEST_BYTES
                ),
            )
            with self.server.mutation_lock:
                if self.server.job_manager.is_project_active(project_id):
                    self._send_error_json(
                        HTTPStatus.CONFLICT,
                        (
                            "OCR prompt editing is unavailable while "
                            "a source job is running."
                            if prompt_kind == "ocr"
                            else (
                                "번역 프롬프트는 백그라운드 작업 중에 "
                                "수정할 수 없습니다."
                            )
                        ),
                        code=(
                            "SOURCE_JOB_CONFLICT"
                            if prompt_kind == "ocr"
                            else "TRANSLATION_JOB_CONFLICT"
                        ),
                    )
                    return
                location = load_workspace_project_id(
                    project_id,
                    self.server.workspace_root,
                )
                if prompt_kind == "ocr":
                    ocr_prompt = request.get("ocr_prompt")
                    if not isinstance(ocr_prompt, str):
                        raise DashboardError("ocr_prompt must be a string.")
                    prompt_path = save_project_ocr_prompt(
                        location,
                        ocr_prompt,
                    )
                    result: dict[str, Any] = {
                        "updated": True,
                        "path": prompt_path.relative_to(
                            location.path
                        ).as_posix(),
                    }
                else:
                    translation_prompt = request.get("translation_prompt")
                    expected_sha256 = request.get("expected_sha256")
                    if not isinstance(translation_prompt, str):
                        raise DashboardError(
                            "translation_prompt must be a string."
                        )
                    if not isinstance(expected_sha256, str):
                        raise DashboardError(
                            "expected_sha256 must be a string."
                        )
                    result = save_project_translation_prompt(
                        location,
                        translation_prompt,
                        expected_sha256=expected_sha256,
                    ).to_dict()
        except ProjectNotFoundError as error:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                error,
                code="RESOURCE_NOT_FOUND",
            )
            return
        except (
            DashboardError,
            SourceRegistrationError,
            TranslationPromptError,
            TypeError,
            ValueError,
        ) as error:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                error,
                code=(
                    "OCR_PROMPT_UPDATE_FAILED"
                    if prompt_kind == "ocr"
                    else "TRANSLATION_PROMPT_UPDATE_FAILED"
                ),
            )
            return
        except (OSError, RuntimeError) as error:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                error,
                code=(
                    "OCR_PROMPT_UPDATE_FAILED"
                    if prompt_kind == "ocr"
                    else "TRANSLATION_PROMPT_UPDATE_FAILED"
                ),
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                f"{prompt_kind}_prompt": result,
            },
        )

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        prefix = "/api/projects/"
        if not path.startswith(prefix) or path == prefix:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if not self._api_authorized():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Invalid review session.",
            )
            return

        project_id = unquote(path[len(prefix) :])
        if "/" in project_id or "\\" in project_id:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "Project ID must not contain path separators.",
                code="PROJECT_DELETE_FAILED",
            )
            return
        if self.server.job_manager.is_project_active(project_id):
            self._send_error_json(
                HTTPStatus.CONFLICT,
                "Project deletion is unavailable while a source job is running.",
                code="SOURCE_JOB_CONFLICT",
            )
            return
        try:
            with self.server.mutation_lock:
                if self.server.job_manager.is_project_active(project_id):
                    self._send_error_json(
                        HTTPStatus.CONFLICT,
                        (
                            "Project deletion is unavailable while "
                            "a source job is running."
                        ),
                        code="SOURCE_JOB_CONFLICT",
                    )
                    return
                location = load_workspace_project_id(
                    project_id,
                    self.server.workspace_root,
                )
                send2trash(str(location.path))
        except ProjectNotFoundError as error:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                error,
                code="RESOURCE_NOT_FOUND",
            )
            return
        except (DashboardError, TypeError, ValueError) as error:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                error,
                code="PROJECT_DELETE_FAILED",
            )
            return
        except (OSError, RuntimeError) as error:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                error,
                code="PROJECT_DELETE_FAILED",
            )
            return

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "project": {
                    "project_id": location.manifest.project_id,
                    "name": location.manifest.name,
                },
            },
        )


def create_dashboard_server(
    *,
    workspace_root: str | Path = "workspaces",
    settings_root: str | Path | None = None,
    source_job_runner: SourceJobRunner | None = None,
    glossary_job_runner: GlossaryJobRunner | None = None,
    translation_job_runner: TranslationJobRunner | None = None,
    port: int = 0,
) -> DashboardHttpServer:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise DashboardError("port must be between 0 and 65535.")
    get_dashboard_document(workspace_root)
    return DashboardHttpServer(
        ("127.0.0.1", port),
        _DashboardHandler,
        workspace_root=workspace_root,
        settings_root=Path.cwd() if settings_root is None else settings_root,
        source_job_runner=source_job_runner,
        glossary_job_runner=glossary_job_runner,
        translation_job_runner=translation_job_runner,
    )


def serve_dashboard(
    *,
    workspace_root: str | Path = "workspaces",
    settings_root: str | Path | None = None,
    port: int = 0,
    open_browser: bool = True,
) -> None:
    server = create_dashboard_server(
        workspace_root=workspace_root,
        settings_root=settings_root,
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
