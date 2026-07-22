"""Command-line entry point for Game Localization Kit."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from glk import __version__
from glk.application.extraction_service import ExtractionError, extract_project_pdf
from glk.application.image_ocr_service import ImageOcrError, ocr_project_images
from glk.application.project_service import create_project, inspect_project
from glk.domain.project import ProjectError
from glk.infrastructure.gemini_layout import GeminiConfigurationError


EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 3
EXIT_PARTIAL = 4


def _add_execution_options(
    parser: argparse.ArgumentParser, *, project_required: bool = False
) -> None:
    parser.add_argument(
        "--project", required=project_required, help="Project ID or workspace path"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show planned work only")
    parser.add_argument("--force", action="store_true", help="Regenerate existing results")
    parser.add_argument("--verbose", action="store_true", help="Enable detailed logging")
    parser.add_argument("--json", action="store_true", help="Print machine-readable output")


def _run_version(_: argparse.Namespace) -> int:
    print(f"glk {__version__}")
    return 0


def _print_error(args: argparse.Namespace, code: str, message: str) -> int:
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"ok": False, "code": code, "message": message},
                ensure_ascii=False,
            )
        )
    else:
        print(f"Error: {message}", file=sys.stderr)
    return EXIT_ERROR


def _run_init(args: argparse.Namespace) -> int:
    try:
        location = create_project(
            name=args.name,
            project_id=args.project_id,
            profile=args.profile,
            source_language=args.source_language,
            target_language=args.target_language,
            workspace_root=args.workspace_root,
            dry_run=args.dry_run,
        )
    except (ProjectError, OSError) as error:
        return _print_error(args, "PROJECT_INIT_FAILED", str(error))

    payload = {
        "ok": True,
        "dry_run": location.dry_run,
        "project_path": str(location.path),
        "manifest": location.manifest.to_dict(),
        "created_paths": list(location.created_paths),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        action = "Would create" if location.dry_run else "Created"
        print(f"{action} project '{location.manifest.project_id}' at {location.path}")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    try:
        status = inspect_project(args.project, args.workspace_root)
    except (ProjectError, OSError) as error:
        return _print_error(args, "PROJECT_STATUS_FAILED", str(error))

    if args.json:
        print(json.dumps(status, ensure_ascii=False))
    else:
        manifest = status["manifest"]
        print(f"Project: {manifest['name']} ({manifest['project_id']})")
        print(f"Path: {status['project_path']}")
        print(f"Profile: {manifest['profile']}")
        print(f"Languages: {manifest['source_language']} -> {manifest['target_language']}")
        print(f"Workspace: {'ready' if status['ok'] else 'incomplete'}")
        if status["missing_paths"]:
            print(f"Missing: {', '.join(status['missing_paths'])}")
    return 0 if status["ok"] else EXIT_ERROR


def _run_extract(args: argparse.Namespace) -> int:
    try:
        result = extract_project_pdf(
            project=args.project,
            file=args.file,
            pages=args.pages,
            workspace_root=args.workspace_root,
            model_name=args.model,
            scale=args.scale,
            force=args.force,
            dry_run=args.dry_run,
            progress=lambda message: print(message, file=sys.stderr),
        )
    except (
        ProjectError,
        ExtractionError,
        GeminiConfigurationError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        return _print_error(args, "EXTRACTION_FAILED", str(error))

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif result.dry_run:
        pages = ", ".join(str(page) for page in result.selected_pages)
        print(f"Would extract pages {pages} from {result.source_pdf}")
    else:
        print(
            f"Extracted {len(result.successful_pages)}/{len(result.selected_pages)} pages "
            f"to {result.output_file}"
        )
        if result.cached_pages:
            print(f"Reused cache: {len(result.cached_pages)} pages")
        for failure in result.failures:
            print(f"Page {failure.page} failed: {failure.error}", file=sys.stderr)
    return 0 if result.ok else EXIT_PARTIAL


def _run_ocr(args: argparse.Namespace) -> int:
    try:
        result = ocr_project_images(
            project=args.project,
            folder=args.folder,
            prompt_file=args.prompt,
            workspace_root=args.workspace_root,
            model_name=args.model,
            force=args.force,
            dry_run=args.dry_run,
            progress=lambda message: print(message, file=sys.stderr),
        )
    except (
        ProjectError,
        ImageOcrError,
        GeminiConfigurationError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        return _print_error(args, "IMAGE_OCR_FAILED", str(error))

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif result.dry_run:
        print(
            f"Would OCR {len(result.selected_images)} images from {result.source_folder}"
        )
    else:
        print(
            f"OCR completed for {len(result.successful_images)}/"
            f"{len(result.selected_images)} images to {result.output_file}"
        )
        if result.cached_images:
            print(f"Reused cache: {len(result.cached_images)} images")
        if result.needs_review:
            print(f"Needs review: {len(result.needs_review)} images")
        for failure in result.failures:
            print(f"{failure.file} failed: {failure.error}", file=sys.stderr)
    return 0 if result.ok else EXIT_PARTIAL


def _run_planned_command(args: argparse.Namespace) -> int:
    command = args.command
    message = (
        f"The 'glk {command}' command interface is ready, but its application "
        "service has not been implemented yet."
    )
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": command,
                    "code": "NOT_IMPLEMENTED",
                    "message": message,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(message, file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glk",
        description="Game localization pipeline for PDF, text, and image sources.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    version_parser = subparsers.add_parser("version", help="Show the installed version")
    version_parser.set_defaults(handler=_run_version)

    init_parser = subparsers.add_parser("init", help="Create a project workspace")
    init_parser.add_argument("name", help="Human-readable project name")
    init_parser.add_argument("--project-id", help="Portable project directory name")
    init_parser.add_argument("--profile", default="default", help="Game configuration profile")
    init_parser.add_argument("--source-language", default="en", help="Source language code")
    init_parser.add_argument("--target-language", default="ko", help="Target language code")
    init_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for project workspaces"
    )
    init_parser.add_argument("--dry-run", action="store_true", help="Show what would be created")
    init_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    init_parser.set_defaults(handler=_run_init)

    run_parser = subparsers.add_parser("run", help="Run the complete localization pipeline")
    run_parser.add_argument("--file", help="Source PDF, text, or image path")
    run_parser.add_argument("--profile", help="Game configuration profile")
    run_parser.add_argument("--resume", action="store_true", help="Resume a previous run")
    _add_execution_options(run_parser)
    run_parser.set_defaults(handler=_run_planned_command)

    extract_parser = subparsers.add_parser(
        "extract", help="Extract and reconstruct source text from a PDF"
    )
    extract_parser.add_argument("--file", help="Source PDF path")
    extract_parser.add_argument("--pages", help="1-based page selection, e.g. 1,3-5")
    extract_parser.add_argument("--model", help="Gemini model override")
    extract_parser.add_argument("--scale", type=float, default=1.5, help="Page render scale")
    extract_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for project workspaces"
    )
    _add_execution_options(extract_parser, project_required=True)
    extract_parser.set_defaults(handler=_run_extract)

    ocr_parser = subparsers.add_parser(
        "ocr", help="Extract source text from an image folder with Gemini OCR"
    )
    ocr_parser.add_argument(
        "--folder", help="Image folder; omit after it has been registered"
    )
    ocr_parser.add_argument(
        "--prompt",
        help="Project-wide OCR prompt; defaults to <folder>/ocr_prompt.txt",
    )
    ocr_parser.add_argument("--model", help="Gemini model override")
    ocr_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for project workspaces"
    )
    _add_execution_options(ocr_parser, project_required=True)
    ocr_parser.set_defaults(handler=_run_ocr)

    glossary_parser = subparsers.add_parser(
        "glossary", help="Analyze or import translation terminology"
    )
    glossary_parser.add_argument("--file", help="Source text or reviewed glossary path")
    _add_execution_options(glossary_parser)
    glossary_parser.set_defaults(handler=_run_planned_command)

    translate_parser = subparsers.add_parser("translate", help="Translate source segments")
    translate_parser.add_argument("--file", help="Source text path")
    translate_parser.add_argument("--resume", action="store_true", help="Resume a previous run")
    _add_execution_options(translate_parser)
    translate_parser.set_defaults(handler=_run_planned_command)

    qa_parser = subparsers.add_parser("qa", help="Validate source and translation integrity")
    _add_execution_options(qa_parser)
    qa_parser.set_defaults(handler=_run_planned_command)

    export_parser = subparsers.add_parser("export", help="Export approved translation output")
    export_parser.add_argument("--output", help="Destination file or directory")
    _add_execution_options(export_parser)
    export_parser.set_defaults(handler=_run_planned_command)

    status_parser = subparsers.add_parser("status", help="Show project pipeline status")
    status_parser.add_argument("--project", required=True, help="Project ID or workspace path")
    status_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for project workspaces"
    )
    status_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    status_parser.set_defaults(handler=_run_status)

    retry_parser = subparsers.add_parser("retry", help="Retry failed pipeline items")
    retry_parser.add_argument("--failed", action="store_true", help="Retry failed items only")
    _add_execution_options(retry_parser)
    retry_parser.set_defaults(handler=_run_planned_command)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return int(handler(args))
