"""Localhost-only HTTP server for the project dashboard."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from importlib import resources
import json
from pathlib import Path
import re
from send2trash import send2trash
import tempfile
import threading
from typing import Any
from urllib.parse import parse_qs, quote
import webbrowser

from glk.application._io import write_bytes_atomic
from glk.application.ai_model_catalog import (
    GeminiModelCatalogError,
    OpenAIModelCatalogError,
    load_ai_model_catalogs,
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
    get_project_dashboard_image_output_archive,
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
from glk.config import resolve_settings_root
from glk.error_response import localized_detail_message
from glk.infrastructure.dashboard_routes import (
    DashboardRoute,
    match_dashboard_route,
    registered_dashboard_route_names,
)
from glk.infrastructure.glossary_review_server import (
    create_glossary_review_server,
)
from glk.infrastructure.local_http import (
    LocalHttpRequestHandler,
    LocalHttpServer,
    validate_local_port,
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
DASHBOARD_DEFAULT_PORT = 8765
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


class DashboardError(ValueError):
    """Raised when the local dashboard cannot be started or used."""


class DashboardRequestError(DashboardError):
    """Raised for a request with stable user-facing recovery guidance."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


class DashboardHttpServer(LocalHttpServer):
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
        self._review_lock = threading.Lock()
        self._review_servers: dict[
            tuple[str, str], tuple[LocalHttpServer, threading.Thread]
        ] = {}
        super().__init__(server_address, handler_class)
        try:
            self.workspace_root = str(workspace_root)
            self.settings_root = Path(settings_root).expanduser().resolve()
            self.ai_settings = AiSettingsService(self.settings_root)
            self.job_manager = DashboardJobManager(
                workspace_root,
                settings_root=self.settings_root,
                runner=source_job_runner,
                glossary_runner=glossary_job_runner,
                translation_runner=translation_job_runner,
            )
        except Exception:
            super().server_close()
            raise

    @property
    def dashboard_url(self) -> str:
        return self.root_url

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

            review_server: LocalHttpServer
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
                    settings_root=self.settings_root,
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
        job_manager = getattr(self, "job_manager", None)
        if job_manager is not None:
            job_manager.close()
        self.close_review_servers()
        super().server_close()


class _DashboardHandler(LocalHttpRequestHandler):
    server: DashboardHttpServer
    request_error_type = DashboardError
    allowed_methods = ("GET", "POST", "PUT", "PATCH", "DELETE")
    registered_route_names = registered_dashboard_route_names()
    handled_route_names = {
        "GET": frozenset(
            {
                "favicon",
                "dashboard_ui",
                "dashboard",
                "jobs",
                "ai_settings",
                "output",
                "output_archive",
            }
        ),
        "POST": frozenset(
            {
                "source_upload",
                "source_job",
                "glossary_job",
                "translation_job",
                "projects",
                "review_open",
            }
        ),
        "PUT": frozenset({"ai_settings", "source_upload"}),
        "PATCH": frozenset({"ocr_prompt", "translation_prompt"}),
        "DELETE": frozenset({"project_delete"}),
    }

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
                raise DashboardRequestError(
                    "OCR_PROMPT_IMAGE_ONLY",
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

    def _route_request(self, method: str) -> DashboardRoute | None:
        route = match_dashboard_route(method, self.path)
        if route is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return None
        if route.access == "localhost" and not self._host_is_local():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Only localhost is allowed.",
                code="LOCAL_ACCESS_REQUIRED",
            )
            return None
        if route.access == "session" and not self._api_authorized():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Invalid review session.",
                code="REVIEW_SESSION_INVALID",
            )
            return None
        if route.name not in self.handled_route_names.get(method, frozenset()):
            self._send_unhandled_route(route)
            return None
        return route

    def _send_unhandled_route(self, route: DashboardRoute) -> None:
        self._send_error_json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"Dashboard route has no handler: {route.method} {route.name}",
            code="INTERNAL_ERROR",
        )

    def _handle_source_upload(
        self,
        project_id: str,
        *,
        replace: bool,
    ) -> None:
        error_code = (
            "SOURCE_REPLACE_FAILED"
            if replace
            else "SOURCE_REGISTER_FAILED"
        )
        if not project_id or "/" in project_id or "\\" in project_id:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "Project ID must not contain path separators.",
                code=error_code,
            )
            return
        if self.server.job_manager.is_project_active(project_id):
            self._send_error_json(
                HTTPStatus.CONFLICT,
                "Source replacement is unavailable while a source job is running.",
                code="SOURCE_JOB_CONFLICT",
            )
            return
        try:
            source_type, uploads, ocr_prompt = self._read_source_upload()
            with self.server.mutation_lock:
                if self.server.job_manager.is_project_active(project_id):
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
                    project_id,
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
                message=localized_detail_message(error),
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
        route = self._route_request("GET")
        if route is None:
            return
        if route.name == "favicon":
            self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        if route.name == "dashboard_ui":
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
        if route.name == "dashboard":
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
        if route.name == "jobs":
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
        if route.name == "ai_settings":
            try:
                settings = self.server.ai_settings.status()
                model_catalogs = load_ai_model_catalogs()
            except (
                AiSettingsError,
                GeminiModelCatalogError,
                OpenAIModelCatalogError,
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
                    "model_catalog": model_catalogs[settings.provider],
                    "model_catalogs": model_catalogs,
                },
            )
            return
        if route.name == "output":
            try:
                query = parse_qs(
                    route.query,
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
        if route.name == "output_archive":
            try:
                query = parse_qs(
                    route.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
                if (
                    set(query) != {"project_id"}
                    or len(query["project_id"]) != 1
                ):
                    raise DashboardOutputError(
                        "프로젝트를 정확히 하나 선택하세요."
                    )
                archive = get_project_dashboard_image_output_archive(
                    project_id=query["project_id"][0],
                    workspace_root=self.server.workspace_root,
                )
            except (DashboardOutputError, OSError, ValueError) as error:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    error,
                    code="OUTPUT_DOWNLOAD_FAILED",
                )
                return
            encoded_name = quote(archive.download_name, safe="")
            self._send_bytes(
                HTTPStatus.OK,
                archive.data,
                "application/zip",
                extra_headers={
                    "Content-Disposition": (
                        "attachment; filename=\"image_outputs.zip\"; "
                        f"filename*=UTF-8''{encoded_name}"
                    ),
                },
            )
            return
        self._send_unhandled_route(route)

    def do_POST(self) -> None:
        route = self._route_request("POST")
        if route is None:
            return
        if route.name == "source_upload":
            assert route.project_id is not None
            self._handle_source_upload(route.project_id, replace=False)
            return
        try:
            request = self._read_request_json(max_bytes=_MAX_REQUEST_BYTES)
            if route.name == "source_job":
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
                        f"{settings.provider.upper()} API key is not configured."
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
            if route.name == "glossary_job":
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
            if route.name == "translation_job":
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
                        f"{settings.provider.upper()} API key is not configured."
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
            if route.name == "projects":
                name = request.get("name")
                project_id = request.get("project_id")
                if not isinstance(name, str) or not name.strip():
                    raise DashboardError("Project name is required.")
                if not isinstance(project_id, str) or not project_id.strip():
                    raise DashboardRequestError(
                        "PROJECT_ID_REQUIRED",
                        "Project ID is required.",
                    )
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
            if route.name != "review_open":
                self._send_unhandled_route(route)
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
                if route.name == "glossary_job"
                else "TRANSLATION_JOB_CONFLICT"
                if route.name == "translation_job"
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
                if route.name == "projects"
                else "SOURCE_JOB_START_FAILED"
                if route.name == "source_job"
                else "GLOSSARY_JOB_START_FAILED"
                if route.name == "glossary_job"
                else "TRANSLATION_JOB_START_FAILED"
                if route.name == "translation_job"
                else "INVALID_REQUEST"
            )
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                error,
                code=code,
                message=localized_detail_message(error),
            )
            return

        self._send_json(HTTPStatus.OK, {"ok": True, "url": url})

    def do_PUT(self) -> None:
        route = self._route_request("PUT")
        if route is None:
            return
        if route.name == "ai_settings":
            try:
                request = self._read_request_json(max_bytes=_MAX_REQUEST_BYTES)
                api_key = request.get("api_key")
                model = request.get("model")
                provider = request.get("provider")
                if api_key is not None and not isinstance(api_key, str):
                    raise DashboardError(
                        "api_key must be a string or null."
                    )
                if not isinstance(model, str):
                    raise DashboardError("model must be a string.")
                if provider is not None and not isinstance(provider, str):
                    raise DashboardError("provider must be a string.")
                with self.server.mutation_lock:
                    settings = self.server.ai_settings.save(
                        api_key=api_key,
                        model=model,
                        provider=provider,
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
        if route.name == "source_upload":
            assert route.project_id is not None
            self._handle_source_upload(route.project_id, replace=True)
            return
        self._send_unhandled_route(route)

    def do_PATCH(self) -> None:
        route = self._route_request("PATCH")
        if route is None:
            return
        if route.name not in {"ocr_prompt", "translation_prompt"}:
            self._send_unhandled_route(route)
            return
        prompt_kind = "ocr" if route.name == "ocr_prompt" else "translation"
        assert route.project_id is not None
        project_id = route.project_id
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
                message=localized_detail_message(error),
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
        route = self._route_request("DELETE")
        if route is None:
            return
        if route.name != "project_delete":
            self._send_unhandled_route(route)
            return
        assert route.project_id is not None
        project_id = route.project_id
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
    validate_local_port(port, error_type=DashboardError)
    get_dashboard_document(workspace_root)
    return DashboardHttpServer(
        ("127.0.0.1", port),
        _DashboardHandler,
        workspace_root=workspace_root,
        settings_root=resolve_settings_root(settings_root),
        source_job_runner=source_job_runner,
        glossary_job_runner=glossary_job_runner,
        translation_job_runner=translation_job_runner,
    )


def serve_dashboard(
    *,
    workspace_root: str | Path = "workspaces",
    settings_root: str | Path | None = None,
    port: int = DASHBOARD_DEFAULT_PORT,
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
