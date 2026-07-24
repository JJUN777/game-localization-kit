"""Command-line entry point for Game Localization Kit."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from glk import __version__
from glk.application.extraction_service import ExtractionError, extract_project_pdf
from glk.application.image_ocr_service import (
    IMAGE_EXTENSIONS,
    ImageOcrError,
    ocr_project_images,
)
from glk.application.glossary_service import (
    GlossaryBuildError,
    GlossaryImportError,
    build_project_glossary_candidates,
    import_project_glossary,
)
from glk.application.glossary_review_service import GlossaryReviewError
from glk.application.project_service import (
    create_project,
    inspect_project,
    list_projects,
    load_project,
)
from glk.application.segmentation_service import SegmentationError, segment_project_source
from glk.application.source_review_service import (
    SourceReviewError,
    finalize_project_source_review,
    prepare_project_source_review,
)
from glk.application.source_qa_service import SourceQaError, run_project_source_qa
from glk.application.translation_service import TranslationError, translate_project
from glk.application.translation_retry_service import retry_failed_translations
from glk.application.translation_review_service import (
    TranslationReviewError,
    finalize_project_translation_review,
    prepare_project_translation_review,
    run_project_translation_qa,
)
from glk.domain.project import ProjectError
from glk.domain.workspace import IMAGE_SOURCE_ROOT, WorkspacePaths, is_pdf_source_file
from glk.infrastructure.gemini_layout import GeminiConfigurationError
from glk.infrastructure.glossary_review_server import serve_glossary_review
from glk.infrastructure.source_review_server import serve_source_review
from glk.infrastructure.translation_review_server import serve_translation_review


EXIT_ERROR = 1
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
        paths = WorkspacePaths(location.path)
        print(f"PDF input: {paths.input_pdf_dir}")
        print(f"Image input: {paths.input_images_dir}")
        print(f"OCR prompt: {paths.input_ocr_prompt}")
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
        glossary_detail = pipeline["glossary_status"]
        if pipeline["glossary_candidates"] is not None:
            glossary_detail += f" ({pipeline['glossary_candidates']} candidates)"
        print(f"Glossary review: {glossary_detail}")
        termbase_detail = pipeline["termbase_status"]
        if pipeline["termbase_entries"] is not None:
            termbase_detail += f" ({pipeline['termbase_entries']} entries)"
        print(f"Termbase: {termbase_detail}")
        translation_detail = pipeline["translation_status"]
        if pipeline["translated_blocks"] is not None:
            translation_detail += f" ({pipeline['translated_blocks']} blocks)"
        print(f"Translation: {translation_detail}")
        review_detail = pipeline["translation_review"]
        if pipeline["translation_qa_issues"] is not None:
            review_detail += f" ({pipeline['translation_qa_issues']} errors)"
        print(f"Translation review: {review_detail}")
        print(
            "Final translation: "
            f"{'approved' if pipeline['final_translation_approved'] else 'not approved'}"
        )
    return 0 if status["ok"] else EXIT_ERROR


def _run_projects(args: argparse.Namespace) -> int:
    try:
        result = list_projects(args.workspace_root)
    except (ProjectError, OSError, ValueError) as error:
        return _print_error(args, "PROJECT_LIST_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        print(f"Workspace: {result.workspace_root}")
        if not result.projects:
            print("No projects found.")
        else:
            headers = ("PROJECT ID", "NAME", "SOURCE", "STAGE", "FINAL", "PATH")
            rows = [
                (
                    project.project_id,
                    project.name,
                    project.source_type or "-",
                    project.stage,
                    "yes" if project.final_translation_approved else "no",
                    project.path,
                )
                for project in result.projects
            ]
            widths = [
                max(len(headers[index]), *(len(row[index]) for row in rows))
                for index in range(len(headers))
            ]
            print("  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
            print("  ".join("-" * width for width in widths))
            for row in rows:
                print(
                    "  ".join(
                        value.ljust(widths[index])
                        for index, value in enumerate(row)
                    )
                )
    for warning in result.warnings:
        print(
            f"Warning: skipped {warning.directory}: {warning.message}",
            file=sys.stderr,
        )
    return 0


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
    if is_pdf_source_file(source_file):
        return "pdf"
    if source_file == IMAGE_SOURCE_ROOT:
        return "images"
    return None


def _default_project_pdf(args: argparse.Namespace) -> str | None:
    location = load_project(args.project, args.workspace_root)
    folder = WorkspacePaths(location.path).input_pdf_dir
    if not folder.is_dir():
        return None
    candidates = sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.casefold() == ".pdf"
        ),
        key=lambda path: path.name.casefold(),
    )
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates[:5])
        raise RunInputError(
            f"Multiple PDFs found in {folder}: {names}. "
            "Keep one PDF there or select one with --file."
        )
    return str(candidates[0]) if candidates else None


def _default_project_image_folder(args: argparse.Namespace) -> str | None:
    location = load_project(args.project, args.workspace_root)
    folder = WorkspacePaths(location.path).input_images_dir
    if not folder.is_dir():
        return None
    if any(
        path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS
        for path in folder.rglob("*")
    ):
        return str(folder)
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
    default_pdf = _default_project_pdf(args)
    default_images = _default_project_image_folder(args)
    if default_pdf and not default_images:
        return "pdf"
    if default_images and not default_pdf:
        return "images"
    if args.json or not sys.stdin.isatty():
        raise RunInputError(
            "Could not determine the input type. Put one PDF in 01_input/pdf, "
            "put images in 01_input/images, or use --input-type."
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
                args.file = _default_project_pdf(args)
            if args.file is None and registered != "pdf":
                if args.json or not sys.stdin.isatty():
                    raise RunInputError(
                        "No PDF found in 01_input/pdf; add one PDF there or provide --file."
                    )
                args.file = _prompt_source_path("pdf")
        else:
            if args.pages:
                raise RunInputError("--pages is only available for PDF extraction.")
            if args.folder is None and registered != "images":
                args.folder = _default_project_image_folder(args)
            if args.folder is None and registered != "images":
                if args.json or not sys.stdin.isatty():
                    raise RunInputError(
                        "No images found in 01_input/images; add images there or "
                        "provide --folder."
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
        payload["next_action"] = "Run without --dry-run to prepare 02_source/review.txt."
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
            "Compare 02_source/draft.txt with the preserved 02_source/review.txt, then "
            "reset explicitly with glk review prepare --force if needed."
        )
    else:
        pipeline_status = "awaiting_human_review"
        next_action = "Edit 02_source/review.txt, then run glk review finalize --dry-run."
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
            f"Would refresh 02_source/draft.txt and {action} 02_source/review.txt "
            f"for {result.total_blocks} blocks"
        )
    elif result.review_created:
        print(f"Prepared editable review TXT at {result.review_file}")
    else:
        print(f"Preserved existing review TXT at {result.review_file}")
        if result.review_status == "stale":
            print(
                "The review TXT is based on an older draft. Compare it with "
                "02_source/draft.txt before using --force.",
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


def _run_glossary_build(args: argparse.Namespace) -> int:
    try:
        result = build_project_glossary_candidates(
            project=args.project,
            workspace_root=args.workspace_root,
            min_frequency=args.min_frequency,
            max_words=args.max_words,
            max_candidates=args.max_candidates,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (ProjectError, GlossaryBuildError, OSError, ValueError) as error:
        return _print_error(args, "GLOSSARY_BUILD_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif result.dry_run and result.status in {"would_create", "would_reset"}:
        action = "reset" if result.status == "would_reset" else "create"
        print(
            f"Would {action} glossary review TSV with "
            f"{result.candidate_count} candidates"
        )
    elif result.status == "stale":
        print(f"Preserved existing glossary review TSV at {result.output_file}")
        print(
            "The approved source or build settings changed. Compare the existing "
            "TSV before resetting it with 'glk glossary build --force'.",
            file=sys.stderr,
        )
    elif result.cached:
        print(
            f"Preserved current glossary review TSV with "
            f"{result.candidate_count} candidates at {result.output_file}"
        )
    else:
        action = "Reset" if result.reset else "Created"
        print(
            f"{action} glossary review TSV with {result.candidate_count} candidates "
            f"at {result.output_file}"
        )
        print("Next: fill translation/status/category and add any missing terms")
    return 0


def _run_glossary_import(args: argparse.Namespace) -> int:
    try:
        result = import_project_glossary(
            project=args.project,
            file=args.file,
            workspace_root=args.workspace_root,
            allow_missing_terms=args.allow_missing_terms,
        )
    except (ProjectError, GlossaryImportError, OSError, ValueError) as error:
        return _print_error(args, "GLOSSARY_IMPORT_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif result.cached:
        print(
            f"Termbase is current with {result.entry_count} entries at "
            f"{result.output_file}"
        )
    else:
        print(
            f"Imported {result.entry_count} glossary entries "
            f"({result.active_count} active, {result.rejected_count} rejected, "
            f"{result.manual_count} manual) to {result.output_file}"
        )
        print(f"Updated review evidence at {result.review_file}")
    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    return 0


def _run_glossary_review_web(args: argparse.Namespace) -> int:
    try:
        serve_glossary_review(
            project=args.project,
            workspace_root=args.workspace_root,
            port=args.port,
            open_browser=not args.no_open,
        )
    except (ProjectError, GlossaryReviewError, OSError, ValueError) as error:
        return _print_error(args, "GLOSSARY_REVIEW_SERVER_FAILED", str(error))
    return 0


def _run_translate(args: argparse.Namespace) -> int:
    try:
        result = translate_project(
            project=args.project,
            workspace_root=args.workspace_root,
            prompt_file=args.prompt,
            model_name=args.model,
            max_characters=args.max_characters,
            resume=args.resume,
            force=args.force,
            dry_run=args.dry_run,
            progress=lambda message: print(message, file=sys.stderr),
        )
    except (
        ProjectError,
        TranslationError,
        GeminiConfigurationError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        return _print_error(args, "TRANSLATION_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif result.dry_run:
        print(
            f"Would translate {result.total_blocks} blocks in "
            f"{result.total_chunks} chunks with {result.model}"
        )
    elif result.cached:
        print(
            f"Translation is current for {result.completed_blocks} blocks at "
            f"{result.output_file}"
        )
    else:
        action = "Resumed and translated" if result.resumed else "Translated"
        print(
            f"{action} {result.completed_blocks} blocks in "
            f"{result.completed_chunks} chunks to {result.output_file}"
        )
        print(f"Draft: {result.draft_file}")
        print(f"Review: {result.review_file} ({result.review_status})")
    return 0


def _run_translation_review_prepare(args: argparse.Namespace) -> int:
    try:
        result = prepare_project_translation_review(
            project=args.project,
            workspace_root=args.workspace_root,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (ProjectError, TranslationReviewError, OSError, ValueError) as error:
        return _print_error(args, "TRANSLATION_REVIEW_PREPARE_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif result.dry_run:
        action = "reset" if result.review_created else "preserve"
        print(
            f"Would {action} 04_translation/review.txt for "
            f"{result.total_blocks} blocks"
        )
    elif result.review_created:
        print(f"Prepared translation review TXT at {result.review_file}")
    else:
        print(f"Preserved translation review TXT at {result.review_file}")
        if result.review_status == "stale":
            print(
                "Compare it with 04_translation/draft.txt, then use --force "
                "only when you intend to reset the review.",
                file=sys.stderr,
            )
    return 0


def _run_translation_review_qa(args: argparse.Namespace) -> int:
    try:
        result = run_project_translation_qa(
            project=args.project,
            workspace_root=args.workspace_root,
            dry_run=args.dry_run,
        )
    except (ProjectError, TranslationReviewError, OSError, ValueError) as error:
        return _print_error(args, "TRANSLATION_QA_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        print(
            f"Translation QA: {result.error_count} errors, "
            f"{result.warning_count} warnings across {result.total_blocks} blocks"
        )
        if result.json_report:
            print(f"JSON report: {result.json_report}")
            print(f"Markdown report: {result.markdown_report}")
    return 0 if result.passed else EXIT_ERROR


def _run_translation_review_finalize(args: argparse.Namespace) -> int:
    try:
        result = finalize_project_translation_review(
            project=args.project,
            workspace_root=args.workspace_root,
            dry_run=args.dry_run,
        )
    except (ProjectError, TranslationReviewError, OSError, ValueError) as error:
        return _print_error(args, "TRANSLATION_FINALIZE_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif not result.valid:
        print(
            f"Translation review failed QA with {result.error_count} errors. "
            f"Report: {result.markdown_report or 'not written in dry-run'}",
            file=sys.stderr,
        )
    elif result.dry_run:
        print(
            f"Translation review is valid: "
            f"{result.changed_blocks}/{result.total_blocks} blocks changed; "
            "no final files written"
        )
    else:
        print(
            f"Finalized {result.total_blocks} translation blocks "
            f"({result.changed_blocks} corrected) to {result.output_file}"
        )
        print(f"Approved segments: {result.approved_segments_file}")
    return 0 if result.valid else EXIT_ERROR


def _run_translation_review_web(args: argparse.Namespace) -> int:
    try:
        serve_translation_review(
            project=args.project,
            workspace_root=args.workspace_root,
            port=args.port,
            open_browser=not args.no_open,
        )
    except (ProjectError, TranslationReviewError, OSError, ValueError) as error:
        return _print_error(args, "TRANSLATION_REVIEW_SERVER_FAILED", str(error))
    return 0


def _run_source_review_web(args: argparse.Namespace) -> int:
    try:
        serve_source_review(
            project=args.project,
            workspace_root=args.workspace_root,
            port=args.port,
            open_browser=not args.no_open,
        )
    except (ProjectError, SourceReviewError, OSError, ValueError) as error:
        return _print_error(args, "SOURCE_REVIEW_SERVER_FAILED", str(error))
    return 0


def _run_retry(args: argparse.Namespace) -> int:
    try:
        result = retry_failed_translations(
            project=args.project,
            workspace_root=args.workspace_root,
            model_name=args.model,
            dry_run=args.dry_run,
            progress=lambda message: print(message, file=sys.stderr),
        )
    except (
        ProjectError,
        TranslationError,
        TranslationReviewError,
        GeminiConfigurationError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        return _print_error(args, "TRANSLATION_RETRY_FAILED", str(error))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif result.dry_run:
        print(
            f"Would retry {result.requested_blocks} QA-error blocks "
            f"with {result.model}"
        )
    elif result.requested_blocks == 0:
        print("No QA-error translation blocks need retranslation.")
    else:
        print(
            f"Retranslated {result.retried_blocks} QA-error blocks; "
            f"{result.remaining_error_count} errors remain"
        )
        print(f"Review: {result.review_file}")
        print(f"Revision: {result.revision_file}")
    return 0 if result.ok else EXIT_ERROR


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
        "glossary", help="Build or import project terminology"
    )
    glossary_subparsers = glossary_parser.add_subparsers(
        dest="glossary_command", metavar="ACTION"
    )
    glossary_build_parser = glossary_subparsers.add_parser(
        "build", help="Create a human-editable glossary candidate TSV"
    )
    glossary_build_parser.add_argument("--project", required=True, help="Project ID or path")
    glossary_build_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    glossary_build_parser.add_argument(
        "--min-frequency", type=int, default=2, help="Minimum repeated-term frequency"
    )
    glossary_build_parser.add_argument(
        "--max-words", type=int, default=4, help="Maximum words in a candidate phrase"
    )
    glossary_build_parser.add_argument(
        "--max-candidates", type=int, default=500, help="Maximum candidate rows"
    )
    glossary_build_parser.add_argument(
        "--force", action="store_true", help="Reset TSV and discard human edits"
    )
    glossary_build_parser.add_argument(
        "--dry-run", action="store_true", help="Analyze without writing files"
    )
    glossary_build_parser.add_argument(
        "--json", action="store_true", help="Print machine-readable output"
    )
    glossary_build_parser.set_defaults(handler=_run_glossary_build)

    glossary_import_parser = glossary_subparsers.add_parser(
        "import", help="Validate reviewed TSV and build the termbase"
    )
    glossary_import_parser.add_argument("--project", required=True, help="Project ID or path")
    glossary_import_parser.add_argument("--file", required=True, help="Reviewed glossary TSV")
    glossary_import_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    glossary_import_parser.add_argument(
        "--allow-missing-terms", action="store_true", help="Allow unverified manual terms"
    )
    glossary_import_parser.add_argument("--json", action="store_true")
    glossary_import_parser.set_defaults(handler=_run_glossary_import)

    translate_parser = subparsers.add_parser(
        "translate", help="Translate approved source blocks with the current termbase"
    )
    translate_parser.add_argument(
        "--prompt", help="Project translation instructions; defaults to 04_translation/prompt.txt"
    )
    translate_parser.add_argument("--model", help="Gemini model override")
    translate_parser.add_argument(
        "--max-characters",
        type=int,
        default=10000,
        help="Maximum source characters per translation chunk",
    )
    translate_parser.add_argument("--resume", action="store_true", help="Resume a previous run")
    translate_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    _add_execution_options(translate_parser, project_required=True)
    translate_parser.set_defaults(handler=_run_translate)

    translation_review_parser = subparsers.add_parser(
        "translation", help="Review, QA, and finalize translated text"
    )
    translation_review_subparsers = translation_review_parser.add_subparsers(
        dest="translation_command", metavar="ACTION"
    )

    translation_prepare_parser = translation_review_subparsers.add_parser(
        "prepare", help="Prepare or deliberately reset translation review TXT"
    )
    translation_prepare_parser.add_argument(
        "--project", required=True, help="Project ID or path"
    )
    translation_prepare_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    translation_prepare_parser.add_argument(
        "--force", action="store_true", help="Reset review TXT from the current draft"
    )
    translation_prepare_parser.add_argument(
        "--dry-run", action="store_true", help="Validate without writing files"
    )
    translation_prepare_parser.add_argument("--json", action="store_true")
    translation_prepare_parser.set_defaults(
        handler=_run_translation_review_prepare
    )

    translation_qa_parser = translation_review_subparsers.add_parser(
        "qa", help="Validate the edited translation and write QA reports"
    )
    translation_qa_parser.add_argument(
        "--project", required=True, help="Project ID or path"
    )
    translation_qa_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    translation_qa_parser.add_argument(
        "--dry-run", action="store_true", help="Validate without writing reports"
    )
    translation_qa_parser.add_argument("--json", action="store_true")
    translation_qa_parser.set_defaults(handler=_run_translation_review_qa)

    translation_finalize_parser = translation_review_subparsers.add_parser(
        "finalize", help="Approve a QA-clean translation review"
    )
    translation_finalize_parser.add_argument(
        "--project", required=True, help="Project ID or path"
    )
    translation_finalize_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    translation_finalize_parser.add_argument(
        "--dry-run", action="store_true", help="Validate without writing final files"
    )
    translation_finalize_parser.add_argument("--json", action="store_true")
    translation_finalize_parser.set_defaults(
        handler=_run_translation_review_finalize
    )

    qa_parser = subparsers.add_parser(
        "qa", help="Run deterministic local QA against review-source blocks"
    )
    qa_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for project workspaces"
    )
    _add_execution_options(qa_parser, project_required=True)
    qa_parser.set_defaults(handler=_run_source_qa)

    review_parser = subparsers.add_parser(
        "review", help="Open source, glossary, or translation review UI"
    )
    review_subparsers = review_parser.add_subparsers(
        dest="review_command", metavar="ACTION"
    )

    review_source_parser = review_subparsers.add_parser(
        "source", help="Open the visual source review UI"
    )
    review_source_parser.add_argument(
        "--project", required=True, help="Project ID or path"
    )
    review_source_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    review_source_parser.add_argument(
        "--port", type=int, default=0, help="Local port; 0 selects an available port"
    )
    review_source_parser.add_argument(
        "--no-open", action="store_true", help="Do not open the default browser"
    )
    review_source_parser.set_defaults(handler=_run_source_review_web)

    review_glossary_parser = review_subparsers.add_parser(
        "glossary", help="Open the glossary review table"
    )
    review_glossary_parser.add_argument(
        "--project", required=True, help="Project ID or path"
    )
    review_glossary_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    review_glossary_parser.add_argument(
        "--port", type=int, default=0, help="Local port; 0 selects an available port"
    )
    review_glossary_parser.add_argument(
        "--no-open", action="store_true", help="Do not open the default browser"
    )
    review_glossary_parser.set_defaults(handler=_run_glossary_review_web)

    review_translation_parser = review_subparsers.add_parser(
        "translation", help="Open the translation review UI"
    )
    review_translation_parser.add_argument(
        "--project", required=True, help="Project ID or path"
    )
    review_translation_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    review_translation_parser.add_argument(
        "--port", type=int, default=0, help="Local port; 0 selects an available port"
    )
    review_translation_parser.add_argument(
        "--no-open", action="store_true", help="Do not open the default browser"
    )
    review_translation_parser.set_defaults(handler=_run_translation_review_web)

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

    status_parser = subparsers.add_parser("status", help="Show project pipeline status")
    status_parser.add_argument("--project", required=True, help="Project ID or workspace path")
    status_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for project workspaces"
    )
    status_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    status_parser.set_defaults(handler=_run_status)

    projects_parser = subparsers.add_parser(
        "projects", help="List projects in the workspace root"
    )
    projects_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for project workspaces"
    )
    projects_parser.add_argument(
        "--json", action="store_true", help="Print machine-readable output"
    )
    projects_parser.set_defaults(handler=_run_projects)

    retry_parser = subparsers.add_parser(
        "retry", help="Retranslate only translation blocks that failed QA"
    )
    retry_parser.add_argument(
        "--failed",
        action="store_true",
        required=True,
        help="Retry QA-error translation blocks only",
    )
    retry_parser.add_argument(
        "--project", required=True, help="Project ID or workspace path"
    )
    retry_parser.add_argument(
        "--workspace-root", default="workspaces", help="Parent directory for workspaces"
    )
    retry_parser.add_argument("--model", help="Gemini model override")
    retry_parser.add_argument(
        "--dry-run", action="store_true", help="List retry targets without calling Gemini"
    )
    retry_parser.add_argument("--verbose", action="store_true", help="Enable detailed logging")
    retry_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    retry_parser.set_defaults(handler=_run_retry)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return int(handler(args))
