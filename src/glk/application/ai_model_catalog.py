"""Load packaged AI model lists used by the dashboard."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
import json
from typing import Any, TypedDict


class GeminiModelOption(TypedDict):
    id: str
    description_ko: str
    recommended: bool


class GeminiModelCatalog(TypedDict):
    schema_version: int
    provider: str
    last_verified: str
    source_url: str
    models: list[GeminiModelOption]


class GeminiModelCatalogError(ValueError):
    """Raised when the packaged Gemini model catalog is invalid."""


@lru_cache(maxsize=1)
def load_gemini_model_catalog() -> GeminiModelCatalog:
    """Return a validated copy of the packaged model catalog."""
    try:
        text = (
            resources.files("glk.data")
            .joinpath("gemini_models.json")
            .read_text(encoding="utf-8")
        )
        value: Any = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GeminiModelCatalogError(
            "Unable to read the Gemini model catalog."
        ) from error
    if not isinstance(value, dict):
        raise GeminiModelCatalogError(
            "Gemini model catalog must be a JSON object."
        )

    schema_version = value.get("schema_version")
    provider = value.get("provider")
    last_verified = value.get("last_verified")
    source_url = value.get("source_url")
    raw_models = value.get("models")
    if (
        schema_version != 1
        or provider != "gemini"
        or not isinstance(last_verified, str)
        or not last_verified
        or not isinstance(source_url, str)
        or not source_url.startswith("https://ai.google.dev/")
        or not isinstance(raw_models, list)
        or not raw_models
    ):
        raise GeminiModelCatalogError(
            "Gemini model catalog metadata is invalid."
        )

    models: list[GeminiModelOption] = []
    seen_ids: set[str] = set()
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise GeminiModelCatalogError(
                "Gemini model catalog entry must be an object."
            )
        model_id = raw_model.get("id")
        description = raw_model.get("description_ko")
        recommended = raw_model.get("recommended")
        if (
            not isinstance(model_id, str)
            or not model_id.startswith("gemini-")
            or model_id in seen_ids
            or not isinstance(description, str)
            or not description
            or not isinstance(recommended, bool)
        ):
            raise GeminiModelCatalogError(
                "Gemini model catalog entry is invalid."
            )
        seen_ids.add(model_id)
        models.append(
            {
                "id": model_id,
                "description_ko": description,
                "recommended": recommended,
            }
        )

    return {
        "schema_version": schema_version,
        "provider": provider,
        "last_verified": last_verified,
        "source_url": source_url,
        "models": models,
    }


class OpenAIModelCatalogError(ValueError):
    """Raised when the packaged OpenAI model catalog is invalid."""


@lru_cache(maxsize=1)
def load_openai_model_catalog() -> GeminiModelCatalog:
    """Return a validated copy of the packaged OpenAI model catalog."""
    try:
        text = (
            resources.files("glk.data")
            .joinpath("openai_models.json")
            .read_text(encoding="utf-8")
        )
        value: Any = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OpenAIModelCatalogError(
            "Unable to read the OpenAI model catalog."
        ) from error
    if not isinstance(value, dict):
        raise OpenAIModelCatalogError(
            "OpenAI model catalog must be a JSON object."
        )

    schema_version = value.get("schema_version")
    provider = value.get("provider")
    last_verified = value.get("last_verified")
    source_url = value.get("source_url")
    raw_models = value.get("models")
    if (
        schema_version != 1
        or provider != "openai"
        or not isinstance(last_verified, str)
        or not last_verified
        or not isinstance(source_url, str)
        or not source_url.startswith("https://developers.openai.com/")
        or not isinstance(raw_models, list)
        or not raw_models
    ):
        raise OpenAIModelCatalogError(
            "OpenAI model catalog metadata is invalid."
        )

    models: list[GeminiModelOption] = []
    seen_ids: set[str] = set()
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise OpenAIModelCatalogError(
                "OpenAI model catalog entry must be an object."
            )
        model_id = raw_model.get("id")
        description = raw_model.get("description_ko")
        recommended = raw_model.get("recommended")
        if (
            not isinstance(model_id, str)
            or not model_id.startswith("gpt-")
            or model_id in seen_ids
            or not isinstance(description, str)
            or not description
            or not isinstance(recommended, bool)
        ):
            raise OpenAIModelCatalogError(
                "OpenAI model catalog entry is invalid."
            )
        seen_ids.add(model_id)
        models.append(
            {
                "id": model_id,
                "description_ko": description,
                "recommended": recommended,
            }
        )
    if sum(model["recommended"] for model in models) != 1:
        raise OpenAIModelCatalogError(
            "OpenAI model catalog must have one recommended model."
        )
    return {
        "schema_version": schema_version,
        "provider": provider,
        "last_verified": last_verified,
        "source_url": source_url,
        "models": models,
    }


def load_ai_model_catalogs() -> dict[str, GeminiModelCatalog]:
    """Return all supported provider catalogs keyed by provider name."""
    return {
        "gemini": load_gemini_model_catalog(),
        "openai": load_openai_model_catalog(),
    }
