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
    FidelityBasis,
    FidelityCoverage,
    FidelityImpact,
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
from localdocforge.security.filenames import sanitize_filename


class EncryptedInputError(PipelineError):
    """The input requires a password; callers may prompt and retry."""


@dataclass(frozen=True)
class _SignatureAssessment:
    """Conservative result of inspecting signature-bearing PDF structures."""

    present: bool = False
    uncertain: bool = False


_MAX_SIGNATURE_GRAPH_OBJECTS = 65_536


def _open_pdf(path: Path, password: str | None):
    import pikepdf

    def checked(pdf):
        issues = list(pdf.check_pdf_syntax())
        if issues:
            pdf.close()
            raise PipelineError(
                f"{path.name!r} has {len(issues)} structural syntax warning(s); "
                "automatic repair is not an implemented operation"
            )
        return pdf

    try:
        return checked(pikepdf.open(path))
    except pikepdf.PasswordError:
        if password:
            try:
                return checked(pikepdf.open(path, password=password))
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
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.KNOWN_LOSS,
            )
        )


def _feature_warnings(
    pdf,
    source_name: str,
    moved_pages: bool,
    *,
    signature_assessment: _SignatureAssessment | None = None,
    include_signature_uncertainty: bool = True,
) -> list[FidelityWarning]:
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
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.KNOWN_LOSS,
            )
        )
    if "/AcroForm" in root:
        warnings.append(
            FidelityWarning(
                code="form-fields-detached",
                message=f"{source_name!r} contains form fields; the output keeps their "
                f"appearance but the interactive field tree is not rebuilt yet",
                severity=WarningSeverity.WARNING,
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.KNOWN_LOSS,
            )
        )
    if "/Names" in root and "/EmbeddedFiles" in root.get("/Names", {}):
        warnings.append(
            FidelityWarning(
                code="attachments-dropped",
                message=f"Embedded file attachments from {source_name!r} are not carried "
                f"into the output by this operation yet",
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.KNOWN_LOSS,
            )
        )
    if "/Metadata" in root:
        warnings.append(
            FidelityWarning(
                code="xmp-metadata-dropped",
                message=f"XMP metadata from {source_name!r} is not carried into the output",
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.KNOWN_LOSS,
            )
        )
    if "/PageLabels" in root:
        warnings.append(
            FidelityWarning(
                code="page-labels-dropped",
                message=f"Page labels from {source_name!r} are not rebuilt in the output",
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.KNOWN_LOSS,
            )
        )
    if "/StructTreeRoot" in root:
        warnings.append(
            FidelityWarning(
                code="tagged-structure-dropped",
                message=(f"Tagged-PDF structure from {source_name!r} is not rebuilt in the output"),
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.KNOWN_LOSS,
            )
        )
    names = root.get("/Names", {})
    if "/Dests" in root or "/Dests" in names:
        warnings.append(
            FidelityWarning(
                code="named-destinations-dropped",
                message=(f"Named destinations from {source_name!r} are not rebuilt in the output"),
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.KNOWN_LOSS,
            )
        )
    if "/OpenAction" in root or "/AA" in root or "/JavaScript" in names:
        warnings.append(
            FidelityWarning(
                code="document-actions-dropped",
                message=(
                    f"Document-level actions/JavaScript from {source_name!r} are not carried "
                    "into the output"
                ),
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.KNOWN_LOSS,
            )
        )
    if signature_assessment is None:
        signature_assessment = _assess_signature_fields(pdf)
    if signature_assessment.present:
        warnings.append(
            FidelityWarning(
                code="signature-semantics-dropped",
                message=(
                    f"Signature fields from {source_name!r} are not validly preserved by "
                    "this page-moving operation"
                ),
                severity=WarningSeverity.CRITICAL,
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.KNOWN_LOSS,
            )
        )
    elif signature_assessment.uncertain and include_signature_uncertainty:
        warnings.append(_signature_presence_uncertain_warning("page-moving operation"))
    if _has_internal_links(pdf):
        warnings.append(
            FidelityWarning(
                code="internal-links-may-break",
                message=(
                    f"Internal page links from {source_name!r} may no longer resolve after "
                    "pages move into a new document"
                ),
                basis=FidelityBasis.STRUCTURAL,
                impact=FidelityImpact.REVIEW,
                remedy="Verify internal links and destinations in the generated PDF.",
            )
        )
    return warnings


def _assess_signature_fields(pdf) -> _SignatureAssessment:
    """Inspect field, widget, and catalog signature structures without failing open."""

    import pikepdf

    uncertain = False
    # Direct pikepdf wrappers have no stable objgen. Keep each wrapper alive so
    # Python cannot recycle its id while the traversal queue is active.
    retained_direct_objects: dict[int, Any] = {}

    def identity(value) -> object:
        objgen = getattr(value, "objgen", (0, 0))
        if objgen != (0, 0):
            return ("objgen", objgen)
        direct_id = id(value)
        retained_direct_objects.setdefault(direct_id, value)
        return ("direct", direct_id)

    try:
        root = pdf.Root
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        return _SignatureAssessment(uncertain=True)

    try:
        has_permissions = "/Perms" in root
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        has_permissions = False
        uncertain = True
    if has_permissions:
        try:
            permissions = root.get("/Perms", {})
            if any(key in permissions for key in ("/DocMDP", "/UR", "/UR3")):
                return _SignatureAssessment(present=True)
            uncertain = True
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            uncertain = True

    pending: list[Any] = []
    queued: set[object] = set()

    def enqueue(value: Any) -> None:
        nonlocal uncertain
        try:
            value_identity = identity(value)
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            uncertain = True
            return
        if value_identity in queued:
            return
        if len(queued) >= _MAX_SIGNATURE_GRAPH_OBJECTS:
            uncertain = True
            return
        queued.add(value_identity)
        pending.append(value)

    def enqueue_collection(values: Any) -> None:
        nonlocal uncertain
        if values is None:
            return
        if not isinstance(values, (pikepdf.Array, list, tuple)):
            uncertain = True
            return
        try:
            for value in values:
                if len(queued) >= _MAX_SIGNATURE_GRAPH_OBJECTS:
                    uncertain = True
                    break
                enqueue(value)
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            uncertain = True

    acroform: Any = {}
    try:
        has_acroform = "/AcroForm" in root
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        has_acroform = False
        uncertain = True
    if has_acroform:
        try:
            candidate_acroform = root.get("/AcroForm")
            if not isinstance(candidate_acroform, pikepdf.Dictionary):
                uncertain = True
            else:
                acroform = candidate_acroform
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            uncertain = True
    try:
        signature_flags = acroform.get("/SigFlags", 0)
        if type(signature_flags) is not int:
            raise TypeError("/SigFlags must be an integer")
        # ISO 32000 defines both low bits as affirmative signature evidence:
        # SignaturesExist (bit 1) and AppendOnly (bit 2). Reserved bits must be
        # zero; a reserved-only or out-of-range value is malformed evidence and
        # therefore cannot support a clean assessment.
        if signature_flags < 0 or signature_flags > 0xFFFF_FFFF:
            uncertain = True
        elif signature_flags & 0b11:
            return _SignatureAssessment(present=True)
        elif signature_flags & ~0b11:
            uncertain = True
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        uncertain = True
    try:
        enqueue_collection(acroform.get("/Fields", []))
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        uncertain = True

    try:
        for page in pdf.pages:
            try:
                enqueue_collection(page.obj.get("/Annots", []))
            except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
                uncertain = True
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        uncertain = True

    while pending:
        value = pending.pop()
        try:
            if str(value.get("/FT", "")) == "/Sig":
                return _SignatureAssessment(present=True)
            if str(value.get("/Type", "")) == "/Sig":
                return _SignatureAssessment(present=True)
            if "/ByteRange" in value and "/Contents" in value:
                return _SignatureAssessment(present=True)
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            uncertain = True
            continue

        for reference_key in ("/V", "/Parent"):
            try:
                referenced = value.get(reference_key)
            except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
                uncertain = True
                continue
            if referenced is not None and referenced is not value:
                enqueue(referenced)
        try:
            enqueue_collection(value.get("/Kids", []))
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            uncertain = True
    return _SignatureAssessment(uncertain=uncertain)


def _has_internal_links(pdf) -> bool:
    """Return whether internal links exist or malformed annotations make that uncertain."""

    import pikepdf

    try:
        pages = pdf.pages
        for page in pages:
            try:
                annotations = list(page.obj.get("/Annots", []))
            except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
                return True
            for annotation in annotations:
                try:
                    action = annotation.get("/A", {})
                    if "/Dest" in annotation or str(action.get("/S", "")) == "/GoTo":
                        return True
                except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
                    return True
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        return True
    return False


def _page_removal_unsafe_features(pdf) -> list[str]:
    """Features whose page references this phase cannot safely rewrite."""
    root = pdf.Root
    unsafe: list[str] = []
    root_features = {
        "/Outlines": "outlines",
        "/AcroForm": "forms/signatures",
        "/PageLabels": "page labels",
        "/OpenAction": "open action",
        "/StructTreeRoot": "tagged structure",
        "/Dests": "named destinations",
    }
    unsafe.extend(label for key, label in root_features.items() if key in root)
    names = root.get("/Names", {})
    if "/Dests" in names:
        unsafe.append("named destinations")
    if _has_internal_links(pdf):
        unsafe.append("internal links")
    return sorted(set(unsafe))


def _signature_invalidated_fidelity_warning(operation: str) -> FidelityWarning:
    return FidelityWarning(
        code="signature-invalidated",
        message=(
            f"{operation} rewrites the PDF, so existing cryptographic signatures no longer "
            "authenticate the generated file."
        ),
        severity=WarningSeverity.CRITICAL,
        basis=FidelityBasis.STRUCTURAL,
        impact=FidelityImpact.KNOWN_LOSS,
        remedy="Retain the signed source as the authoritative copy and sign the output anew.",
    )


def _signature_presence_uncertain_warning(operation: str) -> FidelityWarning:
    return FidelityWarning(
        code="signature-presence-uncertain",
        message=(
            f"{operation} encountered malformed signature-related PDF structures, so "
            "signature preservation could not be assessed completely."
        ),
        basis=FidelityBasis.HEURISTIC,
        impact=FidelityImpact.REVIEW,
        remedy=(
            "Inspect AcroForm fields, widget parent links, and catalog permissions in the "
            "source before relying on the generated file."
        ),
    )


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


def _enforce_page_limit(context: JobContext, page_count: int, *, total: int | None = None) -> None:
    limit = context.limits.max_pages
    measured = page_count if total is None else total
    if limit is not None and measured > limit:
        raise PipelineError(
            f"Inputs total {measured} pages after opening, over the configured limit of {limit}"
        )


def _encryption_removed_warning(source_name: str) -> SecurityWarning:
    return SecurityWarning(
        code="input-encryption-removed",
        message=(
            f"{source_name!r} was password protected. The generated PDF is not password "
            "protected; secure it separately before sharing."
        ),
        severity=WarningSeverity.CRITICAL,
    )


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
        security: list[SecurityWarning] = []
        merged = pikepdf.new()
        total_pages = 0
        total_input_pages = 0
        field_names: set[str] = set()
        conflicting_fields = False
        for index, artifact in enumerate(artifacts):
            context.emit("merge", current=index, total=len(artifacts), message=artifact.path.name)
            with _open_pdf(artifact.path, options.password) as source:
                if source.is_encrypted:
                    security.append(_encryption_removed_warning(artifact.path.name))
                count = len(source.pages)
                total_input_pages += count
                _enforce_page_limit(context, count, total=total_input_pages)
                selection = ranges[index].resolve(count) if ranges[index] else range(1, count + 1)
                for page_number in selection:
                    context.check_cancelled()
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
                    basis=FidelityBasis.STRUCTURAL,
                    impact=FidelityImpact.REVIEW,
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
            fidelity_coverage=FidelityCoverage.PARTIAL,
            security_warnings=security,
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


def _copy_pages_to_new(
    source,
    page_numbers,
    fidelity: list[FidelityWarning],
    context: JobContext,
):
    import pikepdf

    result = pikepdf.new()
    for number in page_numbers:
        context.check_cancelled()
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
    stem = sanitize_filename(input_path.stem, fallback="document")

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        fidelity: list[FidelityWarning] = []
        security: list[SecurityWarning] = []
        with _open_pdf(artifacts[0].path, options.password) as source:
            if source.is_encrypted:
                security.append(_encryption_removed_warning(input_path.name))
            total = len(source.pages)
            _enforce_page_limit(context, total)
            groups: list[tuple[str, list[int]]] = []
            name_occurrences: dict[str, int] = {}

            def unique_name(name: str) -> str:
                occurrence = name_occurrences.get(name, 0) + 1
                name_occurrences[name] = occurrence
                if occurrence == 1:
                    return name
                path = Path(name)
                return f"{path.stem}-repeat-{occurrence:03d}{path.suffix}"

            if pages is not None:
                for token in pages.spec.split(","):
                    token = token.strip()
                    selection = PageRange(spec=token).resolve(total)
                    label = token.replace("-", "_")
                    groups.append((unique_name(f"{stem}-pages-{label}.pdf"), list(selection)))
            elif every is not None:
                for start in range(1, total + 1, every):
                    chunk = list(range(start, min(start + every, total + 1)))
                    groups.append(
                        (
                            unique_name(f"{stem}-part-{(start - 1) // every + 1:03d}.pdf"),
                            chunk,
                        )
                    )
            else:
                groups = [
                    (unique_name(f"{stem}-page-{n:03d}.pdf"), [n]) for n in range(1, total + 1)
                ]

            fidelity.extend(_feature_warnings(source, input_path.name, moved_pages=True))
            candidates: list[CandidateOutput] = []
            for index, (name, numbers) in enumerate(groups):
                context.emit("split", current=index, total=len(groups), message=name)
                part = _copy_pages_to_new(source, numbers, fidelity, context)
                staging = context.workspace / name
                try:
                    part.save(staging)
                finally:
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
            fidelity_coverage=FidelityCoverage.PARTIAL,
            security_warnings=security,
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
    fidelity_coverage: FidelityCoverage = FidelityCoverage.PARTIAL,
) -> ConversionReport:
    """Common shape: one input PDF, one transformed output PDF."""
    engine_name, engine_version = _engine()

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        fidelity: list[FidelityWarning] = []
        security: list[SecurityWarning] = []
        with _open_pdf(artifacts[0].path, options.password) as source:
            if source.is_encrypted:
                security.append(_encryption_removed_warning(input_path.name))
            signature_assessment = _assess_signature_fields(source)
            if signature_assessment.present:
                security.append(
                    SecurityWarning(
                        code="signature-invalidated",
                        message=(
                            f"{operation} rewrites the PDF and invalidates existing "
                            "cryptographic signatures. Signature appearance objects may remain."
                        ),
                        severity=WarningSeverity.CRITICAL,
                    )
                )
                fidelity.append(_signature_invalidated_fidelity_warning(operation))
            elif signature_assessment.uncertain:
                fidelity.append(_signature_presence_uncertain_warning(operation))
            _enforce_page_limit(context, len(source.pages))
            result_pdf, expected_pages = transform(
                source,
                fidelity,
                security,
                context,
                signature_assessment,
            )
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
            fidelity_coverage=fidelity_coverage,
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

    def transform(source, fidelity, security, context, signature_assessment):
        total = len(source.pages)
        to_remove = sorted(set(pages.resolve(total)), reverse=True)
        if len(to_remove) >= total:
            raise PipelineError(
                f"Removing {len(to_remove)} of {total} pages would leave an empty document"
            )
        unsafe = _page_removal_unsafe_features(source)
        if unsafe:
            raise PipelineError(
                "remove-pages refused because this build cannot safely rewrite: "
                + ", ".join(unsafe)
            )
        for number in to_remove:
            context.check_cancelled()
            del source.pages[number - 1]
        return source, total - len(to_remove)

    return _single_output_operation(
        "remove-pages",
        input_path,
        output,
        transform,
        options,
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

    def transform(source, fidelity, security, context, signature_assessment):
        selection = pages.resolve(len(source.pages))
        fidelity.extend(
            _feature_warnings(
                source,
                input_path.name,
                moved_pages=True,
                signature_assessment=signature_assessment,
                include_signature_uncertainty=False,
            )
        )
        result = _copy_pages_to_new(source, selection, fidelity, context)
        return result, len(selection)

    return _single_output_operation(
        "extract-pages",
        input_path,
        output,
        transform,
        options,
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

    def transform(source, fidelity, security, context, signature_assessment):
        selection = order.resolve(len(source.pages))
        fidelity.extend(
            _feature_warnings(
                source,
                input_path.name,
                moved_pages=True,
                signature_assessment=signature_assessment,
                include_signature_uncertainty=False,
            )
        )
        result = _copy_pages_to_new(source, selection, fidelity, context)
        return result, len(selection)

    return _single_output_operation(
        "organize",
        input_path,
        output,
        transform,
        options,
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

    def transform(source, fidelity, security, context, signature_assessment):
        total = len(source.pages)
        for number in set(selection_range.resolve(total)):
            context.check_cancelled()
            source.pages[number - 1].rotate(degrees, relative=True)
        return source, total

    return _single_output_operation(
        "rotate",
        input_path,
        output,
        transform,
        options,
        details={"degrees": degrees, "pages_spec": selection_range.spec},
        fidelity_coverage=FidelityCoverage.COMPLETE,
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

    def transform(source, fidelity, security, context, signature_assessment):
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
            context.check_cancelled()
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
                    f"Crop box does not intersect page {number} (media box is {media})"
                )
            if clamped != (x0, y0, x1, y1):
                fidelity.append(
                    FidelityWarning(
                        code="crop-clamped",
                        message=f"Crop box clamped to the page boundary on page {number}",
                        basis=FidelityBasis.STRUCTURAL,
                        impact=FidelityImpact.REVIEW,
                        page=number,
                    )
                )
            page.obj["/CropBox"] = pikepdf.Array(clamped)
            adjusted += 1
        return source, total

    return _single_output_operation(
        "crop",
        input_path,
        output,
        transform,
        options,
        details={"box": list(box), "pages_spec": selection_range.spec},
        render_all=True,
        fidelity_coverage=FidelityCoverage.COMPLETE,
    )


def inspect_pdf(
    input_path: Path,
    *,
    password: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Read-only structural inventory used by ``ldf inspect`` and reports."""
    import pikepdf

    from localdocforge.security.sniff import require_media_type

    settings = settings or Settings()
    require_media_type(input_path, "application/pdf")
    info: dict[str, Any] = {
        "file": input_path.name,
        "size_bytes": input_path.stat().st_size,
    }
    with _open_pdf(input_path, password) as pdf:
        info["pdf_version"] = str(pdf.pdf_version)
        info["encrypted"] = pdf.is_encrypted
        info["page_count"] = len(pdf.pages)
        page_limit = settings.limits.max_pages
        if page_limit is not None and info["page_count"] > page_limit:
            raise PipelineError(
                f"Input has {info['page_count']} pages, over the configured limit of {page_limit}"
            )
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
    # Keep the structural pikepdf preflight above authoritative, then use the
    # same PDFium extraction policy as pdf-to-md. Only counts leave the helper;
    # inspect never retains or reports source text.
    from localdocforge.operations.text import inspect_page_text_stats

    if info["page_count"] == 0:
        page_text_stats: list[dict[str, object]] = []
        text_coverage: dict[str, object] = {
            "pages_total": 0,
            "pages_with_text": 0,
            "pages_with_text_layer": 0,
            "char_count_min": None,
            "char_count_median": None,
            "char_count_max": None,
        }
    else:
        page_text_stats, text_coverage = inspect_page_text_stats(
            input_path,
            password=password,
            limits=settings.limits,
        )
    info["page_text_stats"] = page_text_stats
    info["text_coverage"] = text_coverage
    return info
