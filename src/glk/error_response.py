"""Consistent, localized errors for CLI and browser-facing boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


_CODE_MESSAGES = {
    "PROJECT_INIT_FAILED": "프로젝트를 생성하지 못했습니다.",
    "PROJECT_STATUS_FAILED": "프로젝트 상태를 확인하지 못했습니다.",
    "PROJECT_LIST_FAILED": "프로젝트 목록을 불러오지 못했습니다.",
    "PROJECT_DELETE_FAILED": "프로젝트를 휴지통으로 이동하지 못했습니다.",
    "PROJECT_ALREADY_EXISTS": "같은 프로젝트 ID가 이미 존재합니다. 다른 프로젝트 ID를 입력하세요.",
    "PROJECT_ID_INVALID": "프로젝트 ID는 영문 소문자, 숫자, 밑줄(_)만 사용할 수 있습니다.",
    "PROJECT_ID_REQUIRED": "프로젝트 ID를 입력하세요.",
    "SOURCE_REGISTER_FAILED": "프로젝트 원본을 등록하지 못했습니다.",
    "SOURCE_REPLACE_FAILED": "프로젝트 원본을 교체하지 못했습니다.",
    "OCR_PROMPT_UPDATE_FAILED": "이미지 OCR 프롬프트를 저장하지 못했습니다.",
    "OCR_PROMPT_EMPTY": "이미지 OCR 프롬프트를 입력하세요.",
    "OCR_PROMPT_INVALID_CHARACTER": "이미지 OCR 프롬프트에 사용할 수 없는 문자가 포함되어 있습니다.",
    "OCR_PROMPT_TOO_LARGE": "이미지 OCR 프롬프트는 64 KiB 이하여야 합니다.",
    "OCR_PROMPT_SOURCE_REQUIRED": "이미지 원본이 등록된 프로젝트에서만 OCR 프롬프트를 수정할 수 있습니다.",
    "OCR_PROMPT_EDIT_LOCKED": "OCR이 시작된 뒤에는 프롬프트를 수정할 수 없습니다.",
    "OCR_PROMPT_IMAGE_ONLY": "OCR 프롬프트는 이미지 원본을 선택했을 때만 사용할 수 있습니다.",
    "AI_SETTINGS_LOAD_FAILED": "AI 설정을 불러오지 못했습니다.",
    "AI_SETTINGS_UPDATE_FAILED": "AI 설정을 저장하지 못했습니다.",
    "GEMINI_API_KEY_MISSING": "Gemini API 키가 없습니다. 대시보드의 AI 설정에서 키를 저장하세요.",
    "GEMINI_API_KEY_OR_REQUEST_INVALID": "Gemini API 키 또는 요청 설정을 확인하세요.",
    "GEMINI_PERMISSION_DENIED": "Gemini 모델을 사용할 권한이 없습니다.",
    "GEMINI_MODEL_NOT_FOUND": "선택한 Gemini 모델을 찾을 수 없습니다. AI 설정에서 모델을 확인하세요.",
    "GEMINI_QUOTA_EXCEEDED": "Gemini API 사용 한도를 초과했습니다. 할당량을 확인한 뒤 다시 시도하세요.",
    "GEMINI_TEMPORARILY_UNAVAILABLE": "Gemini 서비스가 일시적으로 응답하지 않습니다. 잠시 뒤 다시 시도하세요.",
    "GEMINI_NETWORK_ERROR": "Gemini API에 연결하지 못했습니다. 네트워크 상태를 확인하세요.",
    "GEMINI_RESPONSE_EMPTY": "Gemini가 빈 응답을 반환해 결과를 반영하지 않았습니다.",
    "GEMINI_RESPONSE_INVALID": "Gemini 응답 형식이 올바르지 않아 결과를 반영하지 않았습니다.",
    "OPENAI_API_KEY_MISSING": "OpenAI API 키가 없습니다. 대시보드의 AI 설정에서 키를 저장하세요.",
    "OPENAI_API_KEY_OR_REQUEST_INVALID": "OpenAI API 키 또는 요청 설정을 확인하세요.",
    "OPENAI_PERMISSION_DENIED": "OpenAI 모델을 사용할 권한이 없습니다.",
    "OPENAI_MODEL_NOT_FOUND": "선택한 OpenAI 모델을 찾을 수 없습니다. AI 설정에서 모델을 확인하세요.",
    "OPENAI_QUOTA_EXCEEDED": "OpenAI API 사용 한도를 초과했습니다. 결제·한도를 확인한 뒤 다시 시도하세요.",
    "OPENAI_TEMPORARILY_UNAVAILABLE": "OpenAI 서비스가 일시적으로 응답하지 않습니다. 잠시 뒤 다시 시도하세요.",
    "OPENAI_NETWORK_ERROR": "OpenAI API에 연결하지 못했습니다. 네트워크 상태를 확인하세요.",
    "OPENAI_RESPONSE_EMPTY": "OpenAI가 빈 응답을 반환해 결과를 반영하지 않았습니다.",
    "OPENAI_RESPONSE_INVALID": "OpenAI 응답 형식이 올바르지 않아 결과를 반영하지 않았습니다.",
    "SOURCE_JOB_START_FAILED": "원문 준비 작업을 시작하지 못했습니다.",
    "SOURCE_JOB_CONFLICT": "다른 원문 준비 작업이 실행 중입니다.",
    "DASHBOARD_SERVER_FAILED": "통합 대시보드를 열지 못했습니다.",
    "DASHBOARD_PORT_IN_USE": "대시보드 포트를 이미 사용 중입니다. 실행 중인 대시보드를 사용하거나 --port로 다른 포트를 지정하세요.",
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
    "SOURCE_REVIEW_UNRESOLVED_TEXT": "판독 불가 문자나 미확정 아이콘이 남아 있습니다. QA 표시가 있는 블록을 수정하세요.",
    "SOURCE_REVIEW_TOKEN_INVALID": "대괄호 아이콘 token 형식 또는 OCR 프롬프트의 token 정의를 확인하세요.",
    "SOURCE_REVIEW_TOKEN_CONFIRMATION_REQUIRED": "아이콘 token이 변경되었습니다. 원본과 대조한 변경인지 확인하세요.",
    "PDF_ICON_AUDIT_FAILED": "선택한 PDF 블록의 아이콘을 확인하지 못했습니다.",
    "GLOSSARY_BUILD_FAILED": "용어 후보를 만들지 못했습니다.",
    "GLOSSARY_IMPORT_FAILED": "검수한 용어집을 확정하지 못했습니다.",
    "GLOSSARY_GENERATED_CANDIDATE_DELETE": "자동 생성된 용어 후보는 삭제할 수 없습니다. 필요하지 않으면 상태를 제외로 변경하세요.",
    "GLOSSARY_REVIEW_INCOMPLETE": "검토 중인 용어가 남아 있습니다. 상태를 승인·원문 유지·제외 중 하나로 변경하세요.",
    "GLOSSARY_MANUAL_TERMS_MISSING": "원문에서 찾을 수 없는 수동 용어가 있습니다.",
    "GLOSSARY_AI_TRIAGE_FAILED": "AI가 용어 후보를 정리하지 못했습니다.",
    "GLOSSARY_AI_RESPONSE_INVALID": "AI 응답 형식이 올바르지 않아 추천을 반영하지 않았습니다.",
    "GLOSSARY_REVIEW_SERVER_FAILED": "용어 검수 페이지를 열지 못했습니다.",
    "TRANSLATION_FAILED": "초벌 번역에 실패했습니다.",
    "TRANSLATION_PROMPT_AI_FAILED": "AI 번역 프롬프트 초안을 만들지 못했습니다.",
    "TRANSLATION_PROMPT_AI_RESPONSE_INVALID": "AI 응답 형식이 올바르지 않아 프롬프트 초안을 반영하지 않았습니다.",
    "TRANSLATION_REVIEW_PREPARE_FAILED": "번역 검수 파일을 준비하지 못했습니다.",
    "TRANSLATION_QA_FAILED": "번역 자동 QA를 실행하지 못했습니다.",
    "TRANSLATION_FINALIZE_FAILED": "최종 번역본을 만들지 못했습니다.",
    "TRANSLATION_REVIEW_SERVER_FAILED": "번역 검수 페이지를 열지 못했습니다.",
    "TRANSLATION_REVIEW_BLOCK_MISMATCH": "번역 검수 데이터에 알 수 없는 블록이 포함되어 있습니다. 페이지를 새로고침하세요.",
    "TRANSLATION_RETRY_FAILED": "문제가 있는 번역을 다시 번역하지 못했습니다.",
    "TRANSLATION_RETRY_CONFLICT": "오류 문장 재번역 작업이 이미 진행 중입니다.",
    "METHOD_NOT_ALLOWED": "요청한 HTTP 메서드는 지원하지 않습니다.",
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


def localized_detail_message(
    detail: str | BaseException,
) -> str | None:
    """Return guidance carried by an explicit domain error code."""
    if not isinstance(detail, BaseException):
        return None
    code = getattr(detail, "code", None)
    return _CODE_MESSAGES.get(code) if isinstance(code, str) else None


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
        or _CODE_MESSAGES.get(code)
        or "요청을 처리하지 못했습니다."
    )
    return ErrorResponse(code=code, message=localized, detail=detail_text)


def make_http_error_response(
    status: int,
    detail: str | BaseException,
    *,
    code: str | None = None,
    message: str | None = None,
) -> ErrorResponse:
    """Build a browser API error while preserving the original diagnostic."""
    detail_text = str(detail)
    if code is None:
        code = {
            400: "INVALID_REQUEST",
            403: "ACCESS_DENIED",
            404: "RESOURCE_NOT_FOUND",
            409: "REVIEW_CONFLICT",
            500: "INTERNAL_ERROR",
        }.get(int(status), "INTERNAL_ERROR")
    return make_error_response(code, detail_text, message=message)
