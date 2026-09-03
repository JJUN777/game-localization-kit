"""Route matching for the local project dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlsplit


DashboardAccess = Literal["public", "localhost", "session"]


@dataclass(frozen=True, slots=True)
class DashboardRoute:
    """One matched dashboard request and its access policy."""

    name: str
    method: str
    path: str
    query: str
    access: DashboardAccess
    project_id: str | None = None


_STATIC_ROUTES: dict[tuple[str, str], tuple[str, DashboardAccess]] = {
    ("GET", "/favicon.ico"): ("favicon", "public"),
    ("GET", "/"): ("dashboard_ui", "localhost"),
    ("GET", "/api/dashboard"): ("dashboard", "session"),
    ("GET", "/api/jobs"): ("jobs", "session"),
    ("GET", "/api/settings/ai"): ("ai_settings", "session"),
    ("GET", "/api/source-output"): ("source_output", "session"),
    ("GET", "/api/output"): ("output", "session"),
    ("GET", "/api/output-archive"): ("output_archive", "session"),
    ("POST", "/api/projects"): ("projects", "session"),
    ("POST", "/api/review/open"): ("review_open", "session"),
    ("POST", "/api/jobs/source"): ("source_job", "session"),
    ("POST", "/api/jobs/source/continue"): ("source_continue", "session"),
    ("POST", "/api/jobs/glossary"): ("glossary_job", "session"),
    ("POST", "/api/jobs/translation"): ("translation_job", "session"),
    ("PUT", "/api/settings/ai"): ("ai_settings", "session"),
}

_PROJECT_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "source"): "source_upload",
    ("PUT", "source"): "source_upload",
    ("PATCH", "ocr-prompt"): "ocr_prompt",
    ("PATCH", "translation-prompt"): "translation_prompt",
}


def registered_dashboard_route_names() -> dict[str, frozenset[str]]:
    """Return the route names that each dashboard HTTP method can match."""
    names: dict[str, set[str]] = {}
    for (method, _path), (name, _access) in _STATIC_ROUTES.items():
        names.setdefault(method, set()).add(name)
    for (method, _action), name in _PROJECT_ROUTES.items():
        names.setdefault(method, set()).add(name)
    names.setdefault("DELETE", set()).add("project_delete")
    return {
        method: frozenset(method_names)
        for method, method_names in names.items()
    }


def match_dashboard_route(
    method: str,
    request_target: str,
) -> DashboardRoute | None:
    """Return the route for an HTTP method and request target."""
    normalized_method = method.upper()
    parsed = urlsplit(request_target)
    static_route = _STATIC_ROUTES.get((normalized_method, parsed.path))
    if static_route is not None:
        name, access = static_route
        return DashboardRoute(
            name=name,
            method=normalized_method,
            path=parsed.path,
            query=parsed.query,
            access=access,
        )

    prefix = "/api/projects/"
    if not parsed.path.startswith(prefix):
        return None
    remainder = parsed.path[len(prefix) :]
    if not remainder:
        return None

    if normalized_method == "DELETE" and "/" not in remainder:
        return DashboardRoute(
            name="project_delete",
            method=normalized_method,
            path=parsed.path,
            query=parsed.query,
            access="session",
            project_id=unquote(remainder),
        )

    project_part, separator, action = remainder.partition("/")
    if not separator or not project_part or "/" in action:
        return None
    route_name = _PROJECT_ROUTES.get((normalized_method, action))
    if route_name is None:
        return None
    return DashboardRoute(
        name=route_name,
        method=normalized_method,
        path=parsed.path,
        query=parsed.query,
        access="session",
        project_id=unquote(project_part),
    )
