"""Structural PDF operations: merge, split, remove, extract, organize, rotate, crop.

All of these use pikepdf (libqpdf). Sources are opened read-only and results
are written into the job workspace; the pipeline validates and publishes them.

Fidelity policy for this phase: page content, page-level annotations and
links, and document metadata travel with pages. Document-level outlines,
AcroForm field trees, and embedded-file name trees are NOT yet rebuilt when
pages move between documents — whenever an input has them, the operation says
so in a fidelity warning instead of silently dropping them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from localdocforge.config.settings import Settings
from localdocforge.domain.models import (
    ConversionReport,
    FidelityWarning,
    InputArtifact,
    JobContext,
    ProgressCallback,
    SecurityWarning,
    WarningSeverity,
)
from localdocforge.domain.pages import PageRange
from localdocforge.engines.adapters import (
    OP_MERGE,
)
from localdocforge.engines.registry import default_registry
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.pipelines.runner import (
    CandidateOutput,
    ExecuteResult,
    PipelineError,
    run_pipeline,
)


class EncryptedInputError(PipelineError):
    """The input requires a password; callers may prompt and retry."""


def _open_pdf(path: Path, password: str | None):
    import pikepdf

    try:
        return pikepdf.open(path)
    except pikepdf.PasswordError:
        if password:
            try:
                return pikepdf.open(path, password=password)
            except pikepdf.PasswordError as exc:
                raise EncryptedInputError(f"Wrong password for {path.name!r}.") from exc
        raise EncryptedInputError(
            f"{path.name!r} is encrypted. Provide the password to process it."
        ) from None


def _copy_docinfo(source, target, warnings: list[FidelityWarning]) -> None:
    import pikepdf

    try:
        if source.trailer.get("/Info") is not None:
            target.trailer["/Info"] = target.copy_foreign(
                source.make_indirect(source.trailer["/Info"])
            )
    except (pikepdf.PdfError, ValueError, TypeError, KeyError):
        warnings.append(
            FidelityWarning(
                code="docinfo-not-copied",
                message="Document information dictionary could not be copied to the output",
            )
        )


def _feature_warnings(pdf, source_name: str, moved_pages: bool) -> list[FidelityWarning]:
    """Warn about document-level features this phase does not rebuild."""
    if not moved_pages:
        return []
    warnings: list[FidelityWarning] = []
    root = pdf.Root
    if "/Outlines" in root:
        warnings.append(
            FidelityWarning(
                code="outlines-dropped",
                message=f"Bookmarks/outline from {source_name!r} are not carried into the "
                f"output by this operation yet",
            )
        )
    if "/AcroForm" in root:
        warnings.append(
            FidelityWarning(
                code="form-fields-detached",
                message=f"{source_name!r} contains form fields; the output keeps their "
                f"appearance but the interactive field tree is not rebuilt yet",
                severity=WarningSeverity.WARNING,
            )
        )
    if "/Names" in root and "/EmbeddedFiles" in root.get("/Names", {}):
        warnings.append(
            FidelityWarning(
                code="attachments-dropped",
                message=f"Embedded file attachments from {source_name!r} are not carried "
                f"into the output by this operation yet",
            )
        )
    return warnings


@dataclass
class OrganizeOptions:
    """Shared knobs for the structural operations."""

    collision: CollisionPolicy | None = None
    settings: Settings | None = None
    progress: ProgressCallback | None = None
    password: str | None = None


def _engine():
    registry = default_registry()
    engine = registry.engine_for(OP_MERGE)
    info = engine.probe()
    return engine.name, info.version


def merge_pdfs(
    inputs: list[Path],
    output: Path,
    *,
    page_ranges: list[PageRange | None] | None = None,
    options: OrganizeOptions | None = None,
) -> ConversionReport:
    """Merge whole PDFs or selected ranges of each input, in order."""
    import pikepdf

    options = options or OrganizeOptions()
    if len(inputs) < 2 and not (page_ranges and any(page_ranges)):
        # A single input with no range selection would be a copy; require intent.
        if len(inputs) < 2:
            raise PipelineError("merge needs at least two inputs (or use extract-pages)")
    ranges = page_ranges or [None] * len(inputs)
    if len(ranges) != len(inputs):
        raise PipelineError(
            f"Got {len(inputs)} inputs but {len(ranges)} page ranges; they must pair up"
        )

    engine_name, engine_version = _engine()

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        fidelity: list[FidelityWarning] = []
        merged = pikepdf.new()
        total_pages = 0
        field_names: set[str] = set()
        conflicting_fields = False
        for index, artifact in enumerate(artifacts):
            context.emit("merge", current=index, total=len(artifacts), message=artifact.path.name)
            with _open_pdf(artifact.path, options.password) as source:
                count = len(source.pages)
                selection = ranges[index].resolve(count) if ranges[index] else range(1, count + 1)
                for page_number in selection:
                    merged.pages.append(source.pages[page_number - 1])
                    total_pages += 1
                fidelity.extend(_feature_warnings(source, artifact.path.name, moved_pages=True))
                if "/AcroForm" in source.Root:
                    try:
                        for pikepdf_field in source.Root.AcroForm.get("/Fields", []):
                            name = str(pikepdf_field.get("/T", ""))
                            if name and name in field_names:
                                conflicting_fields = True
                            field_names.add(name)
                    except (AttributeError, TypeError):
                        pass
                if index == 0:
                    _copy_docinfo(source, merged, fidelity)
        if conflicting_fields:
            fidelity.append(
                FidelityWarning(
                    code="form-field-name-conflict",
                    message="Inputs contain form fields with identical names; identically "
                    "named fields would have mirrored values if the field tree were kept",
                )
            )
        staging = context.workspace / "merged.pdf"
        merged.save(staging)
        merged.close()
        return ExecuteResult(
            candidates=[
                CandidateOutput(
                    workspace_path=staging,
                    destination=output,
                    expected_pages=total_pages,
                )
            ],
            fidelity_warnings=fidelity,
            output_page_count=total_pages,
            details={"inputs_merged": len(artifacts)},
        )

    return run_pipeline(
        operation="merge",
        input_paths=inputs,
        execute=execute,
        engine_name=engine_name,
        engine_version=engine_version,
        collision=options.collision,
        settings=options.settings,
        progress=options.progress,
        fallback_engine=default_registry().fallback_engine_name(OP_MERGE, engine_name),
    )


def _copy_pages_to_new(source, page_numbers, fidelity: list[FidelityWarning]):
    import pikepdf

    result = pikepdf.new()
    for number in page_numbers:
        result.pages.append(source.pages[number - 1])
    _copy_docinfo(source, result, fidelity)
    return result


def split_pdf(
    input_path: Path,
    output_dir: Path,
    *,
    pages: PageRange | None = None,
    every: int | None = None,
    options: OrganizeOptions | None = None,
) -> ConversionReport:
    """Split into per-token range files, every-N chunks, or single pages.

    - ``pages`` given: one output per comma-separated token ("1-3,7" → 2 files).
    - ``every`` given: consecutive chunks of N pages.
    - neither: one file per page.
    """
    options = options or OrganizeOptions()
    if pages is not None and every is not None:
        raise PipelineError("Choose either --pages or --every, not both")
    if every is not None and every < 1:
        raise PipelineError("--every must be at least 1")
    engine_name, engine_version = _engine()
    stem = input_path.stem

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        fidelity: list[FidelityWarning] = []
        with _open_pdf(artifacts[0].path, options.password) as source:
            total = len(source.pages)
            groups: list[tuple[str, list[int]]] = []
            if pages is not None:
                for token in pages.spec.split(","):
                    token = token.strip()
                    selection = PageRange(spec=token).resolve(total)
                    label = token.replace("-", "_")
                    groups.append((f"{stem}-pages-{label}.pdf", list(selection)))
            elif every is not None:
                for start in range(1, total + 1, every):
                    chunk = list(range(start, min(start + every, total + 1)))
                    groups.append((f"{stem}-part-{(start - 1) // every + 1:03d}.pdf", chunk))
            else:
                groups = [(f"{stem}-page-{n:03d}.pdf", [n]) for n in range(1, total + 1)]

            fidelity.extend(_feature_warnings(source, input_path.name, moved_pages=True))
            candidates: list[CandidateOutput] = []
            for index, (name, numbers) in enumerate(groups):
                context.emit("split", current=index, total=len(groups), message=name)
                part = _copy_pages_to_new(source, numbers, fidelity)
                staging = context.workspace / name
                part.save(staging)
                part.close()
                candidates.append(
                    CandidateOutput(
                        workspace_path=staging,
                        destination=output_dir / name,
                        expected_pages=len(numbers),
                    )
                )
        return ExecuteResult(
            candidates=candidates,
            fidelity_warnings=fidelity,
            details={"parts": len(candidates)},
        )

    return run_pipeline(
        operation="split",
        input_paths=[input_path],
        execute=execute,
        engine_name=engine_name,
        engine_version=engine_version,
        collision=options.collision,
        settings=options.settings,
        progress=options.progress,
    )


def _single_output_operation(
    operation: str,
    input_path: Path,
    output: Path,
    transform,
    options: OrganizeOptions,
    *,
    details: dict[str, Any] | None = None,
    render_all: bool = False,
) -> ConversionReport:
    """Common shape: one input PDF, one transformed output PDF."""
    engine_name, engine_version = _engine()

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        fidelity: list[FidelityWarning] = []
        security: list[SecurityWarning] = []
        with _open_pdf(artifacts[0].path, options.password) as source:
            result_pdf, expected_pages = transform(source, fidelity, security, context)
            staging = context.workspace / f"{operation}.pdf"
            result_pdf.save(staging)
            if result_pdf is not source:
                result_pdf.close()
        return ExecuteResult(
            candidates=[
                CandidateOutput(
                    workspace_path=staging,
                    destination=output,
                    expected_pages=expected_pages,
                    render_all=render_all,
                )
            ],
            fidelity_warnings=fidelity,
            security_warnings=security,
            output_page_count=expected_pages,
            details=details or {},
        )

    return run_pipeline(
        operation=operation,
        input_paths=[input_path],
        execute=execute,
        engine_name=engine_name,
        engine_version=engine_version,
        collision=options.collision,
        settings=options.settings,
        progress=options.progress,
    )


def remove_pages(
    input_path: Path,
    output: Path,
    pages: PageRange,
    *,
    options: OrganizeOptions | None = None,
) -> ConversionReport:
    options = options or OrganizeOptions()

    def transform(source, fidelity, security, context):
        total = len(source.pages)
        to_remove = sorted(set(pages.resolve(total)), reverse=True)
        if len(to_remove) >= total:
            raise PipelineError(
                f"Removing {len(to_remove)} of {total} pages would leave an empty document"
            )
        for number in to_remove:
            del source.pages[number - 1]
        return source, total - len(to_remove)

    return _single_output_operation(
        "remove-pages", input_path, output, transform, options,
        details={"pages_spec": pages.spec},
    )


def extract_pages(
    input_path: Path,
    output: Path,
    pages: PageRange,
    *,
    options: OrganizeOptions | None = None,
) -> ConversionReport:
    options = options or OrganizeOptions()

    def transform(source, fidelity, security, context):
        selection = pages.resolve(len(source.pages))
        fidelity.extend(_feature_warnings(source, input_path.name, moved_pages=True))
        result = _copy_pages_to_new(source, selection, fidelity)
        return result, len(selection)

    return _single_output_operation(
        "extract-pages", input_path, output, transform, options,
        details={"pages_spec": pages.spec},
    )


def organize_pdf(
    input_path: Path,
    output: Path,
    order: PageRange,
    *,
    options: OrganizeOptions | None = None,
) -> ConversionReport:
    """Reorder/duplicate/drop pages according to an explicit new order."""
    options = options or OrganizeOptions()

    def transform(source, fidelity, security, context):
        selection = order.resolve(len(source.pages))
        fidelity.extend(_feature_warnings(source, input_path.name, moved_pages=True))
        result = _copy_pages_to_new(source, selection, fidelity)
        return result, len(selection)

    return _single_output_operation(
        "organize", input_path, output, transform, options,
        details={"order_spec": order.spec},
    )


def rotate_pages(
    input_path: Path,
    output: Path,
    *,
    degrees: int,
    pages: PageRange | None = None,
    options: OrganizeOptions | None = None,
) -> ConversionReport:
    if degrees % 90 != 0:
        raise PipelineError("Rotation must be a multiple of 90 degrees")
    options = options or OrganizeOptions()
    selection_range = pages or PageRange(spec="all")

    def transform(source, fidelity, security, context):
        total = len(source.pages)
        for number in set(selection_range.resolve(total)):
            source.pages[number - 1].rotate(degrees, relative=True)
        return source, total

    return _single_output_operation(
        "rotate", input_path, output, transform, options,
        details={"degrees": degrees, "pages_spec": selection_range.spec},
    )


CROP_NOT_REDACTION = (
    "Cropping only hides content from view. The cropped content is still "
    "inside the file and can be recovered. For true removal use redaction "
    "(planned), never cropping."
)


def crop_pages(
    input_path: Path,
    output: Path,
    *,
    box: tuple[float, float, float, float],
    pages: PageRange | None = None,
    options: OrganizeOptions | None = None,
) -> ConversionReport:
    """Set the CropBox of selected pages to ``box`` (x0, y0, x1, y1 in points)."""
    import pikepdf

    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        raise PipelineError("Crop box must have positive width and height (x0,y0,x1,y1)")
    options = options or OrganizeOptions()
    selection_range = pages or PageRange(spec="all")

    def transform(source, fidelity, security, context):
        security.append(
            SecurityWarning(
                code="crop-is-not-redaction",
                message=CROP_NOT_REDACTION,
                severity=WarningSeverity.WARNING,
            )
        )
        total = len(source.pages)
        adjusted = 0
        for number in set(selection_range.resolve(total)):
            page = source.pages[number - 1]
            media = [float(v) for v in page.mediabox]
            clamped = (
                max(x0, media[0]),
                max(y0, media[1]),
                min(x1, media[2]),
                min(y1, media[3]),
            )
            if clamped[2] <= clamped[0] or clamped[3] <= clamped[1]:
                raise PipelineError(
                    f"Crop box does not intersect page {number} "
                    f"(media box is {media})"
                )
            if clamped != (x0, y0, x1, y1):
                fidelity.append(
                    FidelityWarning(
                        code="crop-clamped",
                        message=f"Crop box clamped to the page boundary on page {number}",
                        page=number,
                    )
                )
            page.obj["/CropBox"] = pikepdf.Array(clamped)
            adjusted += 1
        return source, total

    return _single_output_operation(
        "crop", input_path, output, transform, options,
        details={"box": list(box), "pages_spec": selection_range.spec},
        render_all=True,
    )


def inspect_pdf(input_path: Path, *, password: str | None = None) -> dict[str, Any]:
    """Read-only structural inventory used by ``ldf inspect`` and reports."""
    import pikepdf

    from localdocforge.security.sniff import require_media_type

    require_media_type(input_path, "application/pdf")
    info: dict[str, Any] = {
        "file": input_path.name,
        "size_bytes": input_path.stat().st_size,
    }
    with _open_pdf(input_path, password) as pdf:
        info["pdf_version"] = str(pdf.pdf_version)
        info["encrypted"] = pdf.is_encrypted
        info["page_count"] = len(pdf.pages)
        sizes = set()
        annotation_count = 0
        for page in pdf.pages:
            box = [round(float(v), 2) for v in page.mediabox]
            sizes.add((box[2] - box[0], box[3] - box[1]))
            annots = page.obj.get("/Annots")
            if annots is not None:
                annotation_count += len(annots)
        info["page_sizes_pt"] = sorted([list(size) for size in sizes])
        info["annotations"] = annotation_count
        root = pdf.Root
        info["has_outlines"] = "/Outlines" in root
        info["has_acroform"] = "/AcroForm" in root
        names = root.get("/Names", {})
        info["has_attachments"] = "/EmbeddedFiles" in names
        info["has_javascript"] = "/JavaScript" in names
        info["has_open_action"] = "/OpenAction" in root
        docinfo: dict[str, str] = {}
        try:
            if pdf.trailer.get("/Info") is not None:
                for key, value in pdf.docinfo.items():
                    docinfo[str(key)] = str(value)
        except pikepdf.PdfError:
            pass
        info["docinfo"] = docinfo
    return info
