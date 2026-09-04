"""Shared translation types used by translation application services."""

from __future__ import annotations

from typing import Any, Protocol


DEFAULT_PROJECT_INSTRUCTIONS = """\
게임 규칙서에 어울리는 자연스러운 한국어로 번역하세요.
규칙 본문은 간결하고 일관된 격식체(~합니다)를 사용하세요.
제목과 항목명은 짧고 명확한 명사구로 작성하세요.
영어식 어순과 불필요한 주어 반복을 피하되, 조건·예외·강조를 빠뜨리지 마세요.
동일한 개념과 반복 표현은 문서 전체에서 일관되게 번역하세요."""


class TranslationError(ValueError):
    """Raised when source blocks cannot be translated safely."""


class TranslationValidationError(TranslationError):
    """Raised when a model response violates the translation contract."""

    code = "TRANSLATION_VALIDATION_FAILED"


class TranslationProvider(Protocol):
    model_name: str
    prompt_version: str

    def translate(self, prompt: str) -> dict[str, Any]: ...
