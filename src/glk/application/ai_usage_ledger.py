"""Append-only project ledger for normalized AI usage and estimated cost."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

from glk.application._io import append_bytes_durable
from glk.domain.workspace import WorkspacePaths


AI_USAGE_EVENT_SCHEMA_VERSION = 1
_STAGE_ALIASES: dict[str, str] = {"icon_audit": "source_review"}


def _empty_usage_summary() -> dict[str, Any]:
    return {
        "events": 0,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cached_input_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "unpriced_requests": 0,
        "pricing_complete": True,
        "models": [],
    }


def _add_usage(
    target: dict[str, Any],
    usage: dict[str, Any],
) -> None:
    requests = usage.get("requests")
    if not isinstance(requests, int) or isinstance(requests, bool) or requests <= 0:
        return
    target["events"] += 1
    target["requests"] += requests
    for name in (
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "cached_input_tokens",
    ):
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            target[name] += value
    target["total_tokens"] = target["input_tokens"] + target["output_tokens"]
    cost = usage.get("estimated_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        target["estimated_cost_usd"] += float(cost)
    else:
        target["unpriced_requests"] += requests
        target["pricing_complete"] = False
    model = usage.get("model")
    if isinstance(model, str) and model and model not in target["models"]:
        target["models"].append(model)


def summarize_project_ai_usage(project_path: Path) -> dict[str, Any]:
    """Aggregate valid usage events by stage without breaking dashboard reads."""
    total = _empty_usage_summary()
    stages: dict[str, dict[str, Any]] = {}
    path = WorkspacePaths(project_path).ai_usage_ledger
    if not path.is_file():
        return {"total": total, "stages": stages}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {"total": total, "stages": stages}
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_stage = event.get("stage") if isinstance(event, dict) else None
        if (
            not isinstance(event, dict)
            or event.get("schema_version") != AI_USAGE_EVENT_SCHEMA_VERSION
            or not isinstance(raw_stage, str)
            or not isinstance(event.get("usage"), dict)
        ):
            continue
        stage = _STAGE_ALIASES.get(raw_stage, raw_stage)
        usage = event["usage"]
        _add_usage(stages.setdefault(stage, _empty_usage_summary()), usage)
        _add_usage(total, usage)
    for summary in [total, *stages.values()]:
        summary["estimated_cost_usd"] = round(
            summary["estimated_cost_usd"], 8
        )
        summary["models"].sort()
    return {"total": total, "stages": stages}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def append_ai_usage_event(
    project_path: Path,
    *,
    stage: str,
    operation: str,
    usage: dict[str, Any] | None,
    status: str = "succeeded",
    context: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> bool:
    """Persist one billable operation; zero-request cache hits are omitted."""
    if not isinstance(usage, dict):
        return False
    requests = usage.get("requests")
    if not isinstance(requests, int) or isinstance(requests, bool) or requests <= 0:
        return False
    path = WorkspacePaths(project_path).ai_usage_ledger
    stable_event_id = event_id or uuid.uuid4().hex
    if event_id and path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(existing, dict) and existing.get("event_id") == event_id:
                    return False
        except (OSError, UnicodeDecodeError):
            pass
    event = {
        "schema_version": AI_USAGE_EVENT_SCHEMA_VERSION,
        "event_id": stable_event_id,
        "recorded_at": _utc_now(),
        "stage": stage,
        "operation": operation,
        "status": status,
        "usage": usage,
        "context": context or {},
    }
    append_bytes_durable(
        path,
        (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8"),
    )
    return True
