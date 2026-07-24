"""Consistent, localized errors for CLI and browser-facing boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


_CODE_MESSAGES = {
    "PROJECT_INIT_FAILED": "프로젝트를 생성하지 못했습니다.",
    "PROJECT_STATUS_FAILED": "프로젝트 상태를 확인하지 못했습니다.",
    "PROJECT_LIST_FAILED": "프로젝트 목록을 불러오지 못했습니다.",
    "EXTRACTION_FAILED": "PDF 원문 추출에 실패했습니다.",
    "IMAGE_OCR_FAILED": "이미지 OCR에 실패했습니다.",
    "RUN_INPUT_FAILED": "처리할 원문 입력을 결정하지 못했습니다.",
    "RUN_ACQUISITION_FAILED": "원문을 가져오지 못했습니다.",
    "RUN_PREPARATION_FAILED": "원문 검수 준비에 실패했습니다.",
    "SEGMENTATION_FAILED": "추출 원문을 검수 블록으로 변환하지 못했습니다.",
    "SOURCE_QA_FAILED": "원문 자동 QA를 실행하지 못했습니다.",
    "SOURCE_REVIEW_PREPARE_FAILED": "원문 검수 파일을 준비하지 못했습니다.",
    "SOURCE_REVIEW_FINALIZE_FAILED": "원문 검수를 확정하지 못했습니다.",
    "SOURCE_REVIEW_SERVER_FAILED": "원문 검수 페이지를 열지 못했습니다.",
    "GLOSSARY_BUILD_FAILED": "용어 후보를 만들지 못했습니다.",
    "GLOSSARY_IMPORT_FAILED": "검수한 용어집을 확정하지 못했습니다.",
    "GLOSSARY_REVIEW_SERVER_FAILED": "용어 검수 페이지를 열지 못했습니다.",
    "TRANSLATION_FAILED": "초벌 번역에 실패했습니다.",
    "TRANSLATION_REVIEW_PREPARE_FAILED": "번역 검수 파일을 준비하지 못했습니다.",
    "TRANSLATION_QA_FAILED": "번역 자동 QA를 실행하지 못했습니다.",
    "TRANSLATION_FINALIZE_FAILED": "최종 번역본을 만들지 못했습니다.",
    "TRANSLATION_REVIEW_SERVER_FAILED": "번역 검수 페이지를 열지 못했습니다.",
    "TRANSLATION_RETRY_FAILED": "문제가 있는 번역을 다시 번역하지 못했습니다.",
    "INVALID_REQUEST": "요청 형식이 올바르지 않습니다.",
    "REVIEW_CONFLICT": "다른 곳에서 검수 내용이 변경되었습니다. 페이지를 새로고침한 뒤 다시 시도하세요.",
    "REVIEW_SESSION_INVALID": "검수 세션이 만료되었거나 올바르지 않습니다. 검수 페이지를 다시 여세요.",
    "LOCAL_ACCESS_REQUIRED": "검수 페이지는 이 컴퓨터의 로컬 주소에서만 사용할 수 있습니다.",
    "RESOURCE_NOT_FOUND": "요청한 파일이나 항목을 찾을 수 없습니다.",
    "ACCESS_DENIED": "요청을 처리할 권한이 없습니다.",
    "INTERNAL_ERROR": "내부 처리 중 오류가 발생했습니다.",
}


class ErrorResponsePayload(TypedDict):
    ok: bool
    code: str
    message: str
    detail: str | None


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    code: str
    message: str
    detail: str | None

    def to_dict(self) -> ErrorResponsePayload:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


def _message_for_detail(detail: str) -> str | None:
    normalized = detail.casefold()
    if "changed after" in normalized or "stale" in normalized:
        return _CODE_MESSAGES["REVIEW_CONFLICT"]
    if "invalid review session" in normalized:
        return _CODE_MESSAGES["REVIEW_SESSION_INVALID"]
    if "only localhost is allowed" in normalized:
        return _CODE_MESSAGES["LOCAL_ACCESS_REQUIRED"]
    if "api key" in normalized or "gemini_api_key" in normalized:
        return "Gemini API 키를 확인할 수 없습니다. `.env`의 `GEMINI_API_KEY` 값을 확인하세요."
    if "termbase is not current" in normalized:
        return "용어집이 최신 상태가 아닙니다. 용어 검수를 확정한 뒤 다시 시도하세요."
    if "still in review" in normalized:
        return "검토 중인 용어가 남아 있습니다. 상태를 승인·원문 유지·제외 중 하나로 변경하세요."
    if "cannot be deleted" in normalized:
        return "자동 생성된 용어 후보는 삭제할 수 없습니다. 필요하지 않으면 상태를 제외로 변경하세요."
    if (
        "unknown translation block" in normalized
        or (
            "submitted translation block ids" in normalized
            and "unknown:" in normalized
        )
    ):
        return "번역 검수 데이터에 알 수 없는 블록이 포함되어 있습니다. 페이지를 새로고침하세요."
    if "translation prompt not found" in normalized:
        return "번역 프롬프트 파일을 찾을 수 없습니다. 입력 경로를 확인하세요."
    if "final common source not found" in normalized:
        return "승인된 공통 원문이 없습니다. 원문 검수를 먼저 확정하세요."
    if "no registered pdf" in normalized:
        return "이 프로젝트에 등록된 PDF가 없습니다."
    if "pdf not found" in normalized:
        return "PDF 파일을 찾을 수 없습니다. 입력 경로를 확인하세요."
    if "image folder not found" in normalized:
        return "이미지 폴더를 찾을 수 없습니다. 입력 경로를 확인하세요."
    if "project" in normalized and "not found" in normalized:
        return "프로젝트를 찾을 수 없습니다. 프로젝트 ID와 workspace 경로를 확인하세요."
    if "port must be between" in normalized:
        return "포트 번호는 0부터 65535 사이여야 합니다."
    if (
        "content-type must be" in normalized
        or "invalid content-length" in normalized
        or "request body" in normalized
        or "review_sha256 is required" in normalized
    ):
        return "브라우저 요청 형식이 올바르지 않습니다. 페이지를 새로고침한 뒤 다시 시도하세요."
    return None


def make_error_response(
    code: str,
    detail: str | BaseException | None = None,
    *,
    message: str | None = None,
) -> ErrorResponse:
    """Build a stable error code, Korean user message, and technical detail."""
    detail_text = None if detail is None else str(detail).strip() or None
    localized = (
        message
        or (_message_for_detail(detail_text) if detail_text else None)
        or _CODE_MESSAGES.get(code)
        or "요청을 처리하지 못했습니다."
    )
    return ErrorResponse(code=code, message=localized, detail=detail_text)


def make_http_error_response(
    status: int,
    detail: str | BaseException,
    *,
    code: str | None = None,
) -> ErrorResponse:
    """Build a browser API error while preserving the original diagnostic."""
    detail_text = str(detail)
    normalized = detail_text.casefold()
    if code is None:
        if "changed after" in normalized or "stale" in normalized:
            code = "REVIEW_CONFLICT"
        elif "invalid review session" in normalized:
            code = "REVIEW_SESSION_INVALID"
        elif "only localhost is allowed" in normalized:
            code = "LOCAL_ACCESS_REQUIRED"
        else:
            code = {
                400: "INVALID_REQUEST",
                403: "ACCESS_DENIED",
                404: "RESOURCE_NOT_FOUND",
                409: "REVIEW_CONFLICT",
                500: "INTERNAL_ERROR",
            }.get(int(status), "INTERNAL_ERROR")
    return make_error_response(code, detail_text)
