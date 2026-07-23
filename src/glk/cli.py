"""Command-line entry point for Game Localization Kit."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from glk import __version__
from glk.application.extraction_service import ExtractionError, extract_project_pdf
from glk.application.image_ocr_service import ImageOcrError, ocr_project_images
from glk.application.project_service import create_project, inspect_project, load_project
from glk.application.segmentation_service import SegmentationError, segment_project_source
from glk.application.source_review_service import (
    SourceReviewError,
    finalize_project_source_review,
    prepare_project_source_review,
)
from glk.application.source_qa_service import SourceQaError, run_project_source_qa
from glk.domain.project import ProjectError
from glk.infrastructure.gemini_layout import GeminiConfigurationError


EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 3
EXIT_PARTIAL = 4


class RunInputError(ValueError):
    """Raised when `glk run` cannot determine a valid source input."""


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
        pipeline = status["pipeline"]
        print(f"Source acquired: {'yes' if pipeline['source_acquired'] else 'no'}")
        print(
            "Review source: "
            f"{'ready' if pipeline['review_source_ready'] else 'not ready'}"
        )
        qa_detail = pipeline["qa_status"]
        if pipeline["qa_issues"] is not None:
            qa_detail += f" ({pipeline['qa_issues']} issues)"
        print(f"Source QA: {qa_detail}")
        print(f"Human review: {pipeline['human_review']}")
        print(
            "Final source: "
            f"{'approved' if pipeline['final_source_approved'] else 'not approved'}"
        )
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


def _strip_matching_quotes(value: str) -> str:
    clean = value.strip()
    if len(clean) >= 2 and clean[0] == clean[-1] and clean[0] in {"'", '"'}:
        return clean[1:-1].strip()
    return clean


def _prompt_source_type() -> str:
    print("원문 입력 방식을 선택하세요.")
    print("1. PDF를 기반으로 원문 TXT 추출")
    print("2. 이미지 폴더를 기반으로 원문 OCR")
    while True:
        try:
            choice = input("선택 [1-2]: ").strip().casefold()
        except EOFError as error:
            raise RunInputError(
                "Interactive input is unavailable; provide --input-type."
            ) from error
        if choice in {"1", "pdf"}:
            return "pdf"
        if choice in {"2", "image", "images", "ocr"}:
            return "images"
        print("1 또는 2를 입력하세요.", file=sys.stderr)


def _prompt_source_path(input_type: str) -> str:
    label = "PDF 파일 경로" if input_type == "pdf" else "이미지 루트 폴더 경로"
    try:
        value = _strip_matching_quotes(input(f"{label}: "))
    except EOFError as error:
        raise RunInputError(
            f"Interactive input is unavailable; provide "
            f"{'--file' if input_type == 'pdf' else '--folder'}."
        ) from error
    if not value:
        raise RunInputError(f"{label}를 입력해야 합니다.")
    return value


def _registered_input_type(args: argparse.Namespace) -> str | None:
    location = load_project(args.project, args.workspace_root)
    source_file = location.manifest.source_file
    if source_file == "source/original.pdf":
        return "pdf"
    if source_file == "source/images":
        return "images"
    return None


def _resolve_run_input_type(args: argparse.Namespace) -> str:
    if args.file and args.folder:
        raise RunInputError("Use either --file or --folder, not both.")
    if args.input_type == "pdf" and args.folder:
        raise RunInputError("--folder cannot be used with --input-type pdf.")
    if args.input_type == "images" and args.file:
        raise RunInputError("--file cannot be used with --input-type images.")
    if args.input_type:
        return str(args.input_type)
    if args.file:
        return "pdf"
    if args.folder:
        return "images"
    registered = _registered_input_type(args)
    if registered:
        return registered
    if args.json or not sys.stdin.isatty():
        raise RunInputError(
            "Input type is required in non-interactive mode; use "
            "--input-type pdf or --input-type images."
        )
    return _prompt_source_type()


def _run_pipeline(args: argparse.Namespace) -> int:
    try:
        input_type = _resolve_run_input_type(args)
        registered = _registered_input_type(args)
        if registered and registered != input_type and not args.force:
            raise RunInputError(
                f"Project source is already registered as {registered}. "
                "Use --force to replace the source type."
            )
        if input_type == "pdf":
            if args.prompt:
                raise RunInputError("--prompt is only available for image OCR.")
            if args.file is None and registered != "pdf":
                if args.json or not sys.stdin.isatty():
                    raise RunInputError(
                        "PDF file is required; provide --file or use interactive mode."
                    )
                args.file = _prompt_source_path("pdf")
        else:
            if args.pages:
                raise RunInputError("--pages is only available for PDF extraction.")
            if args.folder is None and registered != "images":
                if args.json or not sys.stdin.isatty():
                    raise RunInputError(
                        "Image folder is required; provide --folder or use interactive mode."
                    )
                args.folder = _prompt_source_path("images")
    except (ProjectError, RunInputError, OSError) as error:
        return _print_error(args, "RUN_INPUT_FAILED", str(error))

    try:
        if input_type == "pdf":
            acquisition = extract_project_pdf(
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
        else:
            acquisition = ocr_project_images(
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
        ExtractionError,
        ImageOcrError,
        GeminiConfigurationError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        return _print_error(args, "RUN_ACQUISITION_FAILED", str(error))

    acquisition_payload = acquisition.to_dict()
    payload = dict(acquisition_payload)
    payload.update(
        {
            "input_type": input_type,
            "acquisition": acquisition_payload,
            "segmentation": None,
            "qa": None,
        }
    )
    if acquisition.dry_run:
        payload["planned_stages"] = ["acquire", "segment", "qa", "human_review"]
        payload["next_action"] = "Run without --dry-run to prepare review/source.txt."
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        elif input_type == "pdf":
            pages = ", ".join(str(page) for page in acquisition.selected_pages)
            print(f"Would extract pages {pages} from {acquisition.source_pdf}")
            print("Would then prepare review TXT and run local source QA")
        else:
            print(
                f"Would OCR {len(acquisition.selected_images)} images from "
                f"{acquisition.source_folder}"
            )
            print("Would then prepare review TXT and run local source QA")
        return 0

    if not acquisition.ok:
        payload["ok"] = False
        payload["pipeline_status"] = "acquisition_partial"
        payload["next_action"] = "Resolve acquisition failures and rerun glk run."
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print("Source acquisition was partial; review preparation was not started.")
            for failure in acquisition.failures:
                target = getattr(failure, "page", getattr(failure, "file", "unknown"))
                print(f"{target} failed: {failure.error}", file=sys.stderr)
        return EXIT_PARTIAL

    try:
        segmentation = segment_project_source(
            project=args.project,
            workspace_root=args.workspace_root,
            force=args.force,
        )
        qa = run_project_source_qa(
            project=args.project,
            workspace_root=args.workspace_root,
            force=args.force,
        )
    except (
        ProjectError,
        SegmentationError,
        SourceQaError,
        SourceReviewError,
        OSError,
        ValueError,
    ) as error:
        return _print_error(args, "RUN_PREPARATION_FAILED", str(error))

    current_pipeline = inspect_project(args.project, args.workspace_root)["pipeline"]
    if current_pipeline["final_source_approved"]:
        pipeline_status = "approved"
        next_action = "Final common source is already approved."
    elif segmentation.review_status == "stale":
        pipeline_status = "review_stale"
        next_action = (
            "Compare draft/source.txt with the preserved review/source.txt, then "
            "reset explicitly with glk review prepare --force if needed."
        )
    else:
        pipeline_status = "awaiting_human_review"
        next_action = "Edit review/source.txt, then run glk review finalize --dry-run."
    payload.update(
        {
            "ok": True,
            "pipeline_status": pipeline_status,
            "segmentation": segmentation.to_dict(),
            "qa": qa.to_dict(),
            "review_file": segmentation.review_file,
            "qa_report_file": qa.human_report_file,
            "next_action": next_action,
        }
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if input_type == "pdf":
            print(
                f"Source acquisition: {len(acquisition.successful_pages)}/"
                f"{len(acquisition.selected_pages)} PDF pages"
            )
        else:
            print(
                f"Source acquisition: {len(acquisition.successful_images)}/"
                f"{len(acquisition.selected_images)} images"
            )
        print(f"Review source: {segmentation.total_blocks} blocks")
        print(
            f"Local QA: {qa.total_issues} issues in "
            f"{qa.flagged_blocks}/{qa.total_blocks} blocks"
        )
        print(f"QA report: {qa.human_report_file}")
        print(f"Editable review TXT: {segmentation.review_file}")
        if pipeline_status == "approved":
            print("Final common source is already approved")
        elif segmentation.review_status == "stale":
            print(f"Warning: {next_action}", file=sys.stderr)
        else:
            print("Next: edit the review TXT, then run glk review finalize --dry-run")
    return 0


def _run_segment(args: argparse.Namespace) -> int:
    try:
        result = segment_project_source(
            project=args.project,
            workspace_root=args.workspace_root,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (ProjectError, SegmentationError, OSError, ValueError) as error:
        return _print_error(args, "SEGMENTATION_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif result.dry_run:
        print(
            f"Would prepare {result.total_blocks} {result.source_type} review-source blocks "
            f"({result.flagged_blocks} flagged)"
        )
    elif result.cached:
        print(
            f"Reused {result.total_blocks} review-source blocks from "
            f"{result.output_file}"
        )
    else:
        print(
            f"Prepared {result.total_blocks} {result.source_type} review-source blocks "
            f"({result.flagged_blocks} flagged) at {result.output_file}"
        )
    if not args.json and not result.dry_run and result.review_file:
        if result.review_created:
            print(f"Editable review TXT: {result.review_file}")
        elif result.review_status == "stale":
            print(
                "Review TXT was preserved but is stale; compare it with the new draft "
                "before resetting it with 'glk review prepare --force'.",
                file=sys.stderr,
            )
    return 0


def _run_source_qa(args: argparse.Namespace) -> int:
    try:
        result = run_project_source_qa(
            project=args.project,
            workspace_root=args.workspace_root,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (ProjectError, SourceQaError, OSError, ValueError) as error:
        return _print_error(args, "SOURCE_QA_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        action = "Would report" if result.dry_run else "Source QA found"
        print(
            f"{action} {result.total_issues} issues in {result.flagged_blocks}/"
            f"{result.total_blocks} blocks "
            f"({result.error_count} errors, {result.warning_count} warnings, "
            f"{result.info_count} info)"
        )
        if result.cached:
            print("Reused source QA cache")
        elif result.human_report_file or result.output_file:
            print(f"Report: {result.human_report_file or result.output_file}")
    return 0


def _run_review_prepare(args: argparse.Namespace) -> int:
    try:
        result = prepare_project_source_review(
            project=args.project,
            workspace_root=args.workspace_root,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (ProjectError, SourceReviewError, OSError, ValueError) as error:
        return _print_error(args, "SOURCE_REVIEW_PREPARE_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif result.dry_run:
        action = "reset" if result.review_created else "preserve"
        print(
            f"Would refresh draft/source.txt and {action} review/source.txt "
            f"for {result.total_blocks} blocks"
        )
    elif result.review_created:
        print(f"Prepared editable review TXT at {result.review_file}")
    else:
        print(f"Preserved existing review TXT at {result.review_file}")
        if result.review_status == "stale":
            print(
                "The review TXT is based on an older draft. Compare it with "
                "draft/source.txt before using --force.",
                file=sys.stderr,
            )
    return 0


def _run_review_finalize(args: argparse.Namespace) -> int:
    try:
        result = finalize_project_source_review(
            project=args.project,
            workspace_root=args.workspace_root,
            allow_token_changes=args.allow_token_changes,
            dry_run=args.dry_run,
        )
    except (ProjectError, SourceReviewError, OSError, ValueError) as error:
        return _print_error(args, "SOURCE_REVIEW_FINALIZE_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif result.dry_run:
        print(
            f"Review TXT is valid: {result.changed_blocks}/{result.total_blocks} "
            "blocks changed; no files written"
        )
    else:
        print(
            f"Finalized {result.total_blocks} blocks "
            f"({result.changed_blocks} corrected) to {result.output_file}"
        )
        print(f"Approved blocks: {result.approved_blocks_file}")
    return 0


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

    run_parser = subparsers.add_parser(
        "run", help="Acquire source, prepare review TXT, and run local QA"
    )
    run_parser.add_argument(
        "--input-type", choices=("pdf", "images"), help="Source type for non-interactive use"
    )
    run_parser.add_argument("--file", help="Source PDF path")
    run_parser.add_argument("--folder", help="Image root folder; subfolders are preserved")
    run_parser.add_argument(
        "--prompt",
        help="Image OCR prompt; defaults to <folder>/ocr_prompt.txt",
    )
    run_parser.add_argument("--pages", help="Optional PDF page selection, e.g. 1,3-5")
    run_parser.add_argument("--model", help="Gemini model override")
    run_parser.add_argument("--scale", type=float, default=1.5, help="PDF render scale")
    run_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for project workspaces"
    )
    _add_execution_options(run_parser, project_required=True)
    run_parser.set_defaults(handler=_run_pipeline)

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

    segment_parser = subparsers.add_parser(
        "segment", help="Prepare normalized source blocks for QA and human review"
    )
    segment_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for project workspaces"
    )
    _add_execution_options(segment_parser, project_required=True)
    segment_parser.set_defaults(handler=_run_segment)

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

    qa_parser = subparsers.add_parser(
        "qa", help="Run deterministic local QA against review-source blocks"
    )
    qa_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for project workspaces"
    )
    _add_execution_options(qa_parser, project_required=True)
    qa_parser.set_defaults(handler=_run_source_qa)

    review_parser = subparsers.add_parser(
        "review", help="Prepare or finalize the human-editable source TXT"
    )
    review_subparsers = review_parser.add_subparsers(
        dest="review_command", metavar="ACTION"
    )

    review_prepare_parser = review_subparsers.add_parser(
        "prepare", help="Create draft and editable review TXT files"
    )
    review_prepare_parser.add_argument("--project", required=True, help="Project ID or path")
    review_prepare_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    review_prepare_parser.add_argument(
        "--force", action="store_true", help="Reset review TXT from the current draft"
    )
    review_prepare_parser.add_argument(
        "--dry-run", action="store_true", help="Validate without writing files"
    )
    review_prepare_parser.add_argument(
        "--json", action="store_true", help="Print machine-readable output"
    )
    review_prepare_parser.set_defaults(handler=_run_review_prepare)

    review_finalize_parser = review_subparsers.add_parser(
        "finalize", help="Validate reviewed text and create final source files"
    )
    review_finalize_parser.add_argument("--project", required=True, help="Project ID or path")
    review_finalize_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    review_finalize_parser.add_argument(
        "--allow-token-changes",
        action="store_true",
        help="Explicitly permit changes to {ICON} token counts",
    )
    review_finalize_parser.add_argument(
        "--dry-run", action="store_true", help="Validate without writing final files"
    )
    review_finalize_parser.add_argument(
        "--json", action="store_true", help="Print machine-readable output"
    )
    review_finalize_parser.set_defaults(handler=_run_review_finalize)

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
