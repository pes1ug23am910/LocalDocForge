"""Optimization operations. Current slice: lossless PDF compression.

The implemented ``lossless`` preset rewrites the document container without
touching decoded content: streams are recompressed (generalized filters only —
never DCT/JPX image data), object streams are generated, and unreferenced
page resources are pruned when qpdf can do so safely. Because the operation
saves the same in-memory document it opened, document-level structures
(outlines, forms, attachments, metadata, page labels) are preserved — unlike
page-moving operations.

Honesty contract: sampled pages of every candidate are rendered through
PDFium and compared pixel-for-pixel against the source before publication.
A lossless preset that changes rendered pixels is a defect, so any
difference fails the job closed. Files that are already tightly compressed
may not shrink; that outcome is reported, never hidden. The lossy
image-downsampling presets from the specification (``balanced``,
``aggressive``, ``archival``) are not implemented and are refused loudly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from localdocforge.domain.models import (
    ConversionReport,
    FidelityWarning,
    InputArtifact,
    JobContext,
    SecurityWarning,
    WarningSeverity,
)
from localdocforge.engines.adapters import OP_COMPRESS
from localdocforge.engines.registry import default_registry
from localdocforge.operations.organize import (
    OrganizeOptions,
    _encryption_removed_warning,
    _enforce_page_limit,
    _has_signature_fields,
    _open_pdf,
)
from localdocforge.pipelines.runner import (
    CandidateOutput,
    ExecuteResult,
    PipelineError,
    run_pipeline,
)
from localdocforge.validation.pdf_checks import render_pdf_page

#: Presets whose pipeline exists and is tested in this build.
COMPRESS_PRESETS: tuple[str, ...] = ("lossless",)
#: Specification presets that are not implemented yet and must be refused.
PLANNED_COMPRESS_PRESETS: tuple[str, ...] = ("balanced", "aggressive", "archival")

_COMPARE_SAMPLE_LIMIT = 5  # pages compared pixel-for-pixel against the source
_COMPARE_SCALE = 1.0  # ~72 dpi; comparison operates on identical render settings


def _engine() -> tuple[str, str | None]:
    registry = default_registry()
    engine = registry.engine_for(OP_COMPRESS)
    info = engine.probe()
    return engine.name, info.version


def _sampled_page_indices(page_count: int, limit: int) -> list[int]:
    """Evenly sample up to ``limit`` zero-based pages, always including both ends."""
    if page_count <= limit:
        return list(range(page_count))
    if limit == 1:
        return [0]
    return sorted({round(i * (page_count - 1) / (limit - 1)) for i in range(limit)})


def compare_page_renders(
    source_path: Path,
    candidate_path: Path,
    page_count: int,
    *,
    source_password: str | None = None,
    sample_limit: int = _COMPARE_SAMPLE_LIMIT,
    context: JobContext | None = None,
) -> dict[str, Any]:
    """Render sampled pages of both files identically and compare pixels.

    Returns a JSON-safe summary: which 1-based pages were compared, whether
    every compared page was pixel-identical, the pages that differed, and the
    largest per-channel delta observed (0 when identical).
    """
    from PIL import ImageChops

    compared: list[int] = []
    mismatched: list[int] = []
    max_delta = 0
    for index in _sampled_page_indices(page_count, sample_limit):
        if context is not None:
            context.check_cancelled()
        source_image = render_pdf_page(
            source_path, index, scale=_COMPARE_SCALE, password=source_password
        )
        try:
            candidate_image = render_pdf_page(candidate_path, index, scale=_COMPARE_SCALE)
            try:
                compared.append(index + 1)
                if source_image.size != candidate_image.size:
                    mismatched.append(index + 1)
                    max_delta = 255
                    continue
                difference = ImageChops.difference(
                    source_image.convert("RGB"), candidate_image.convert("RGB")
                )
                extrema = difference.getextrema()
                # Pillow returns (min, max) for single-band images and a tuple
                # of per-band pairs for multi-band ones; RGB gives the latter.
                bands = extrema if isinstance(extrema[0], tuple) else (extrema,)
                delta = max(int(high) for _low, high in bands)
                max_delta = max(max_delta, delta)
                if delta > 0:
                    mismatched.append(index + 1)
            finally:
                candidate_image.close()
        finally:
            source_image.close()
    return {
        "pages_compared": compared,
        "identical": not mismatched,
        "mismatched_pages": mismatched,
        "max_channel_delta": max_delta,
    }


def compress_pdf(
    input_path: Path,
    output: Path,
    *,
    preset: str = "lossless",
    options: OrganizeOptions | None = None,
) -> ConversionReport:
    """Losslessly optimize a PDF's structure. Never re-encodes image data."""
    import pikepdf

    options = options or OrganizeOptions()
    if preset not in COMPRESS_PRESETS:
        if preset in PLANNED_COMPRESS_PRESETS:
            raise PipelineError(
                f"Compression preset {preset!r} is not implemented in this build. "
                f"Available: {', '.join(COMPRESS_PRESETS)}. Image-downsampling presets "
                "are planned and will never be advertised before they exist."
            )
        raise PipelineError(
            f"Unknown compression preset {preset!r}. Available: {', '.join(COMPRESS_PRESETS)}"
        )

    engine_name, engine_version = _engine()

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        fidelity: list[FidelityWarning] = []
        security: list[SecurityWarning] = []
        source_path = artifacts[0].path
        with _open_pdf(source_path, options.password) as source:
            if source.is_encrypted:
                security.append(_encryption_removed_warning(input_path.name))
            if _has_signature_fields(source):
                security.append(
                    SecurityWarning(
                        code="signature-invalidated",
                        message=(
                            "compress rewrites the PDF and invalidates existing "
                            "cryptographic signatures. Signature appearance objects may remain."
                        ),
                        severity=WarningSeverity.CRITICAL,
                    )
                )
            total = len(source.pages)
            _enforce_page_limit(context, total)
            context.emit("compress", total=total, message="rewriting document structure")
            try:
                source.remove_unreferenced_resources()
            except pikepdf.PdfError:
                fidelity.append(
                    FidelityWarning(
                        code="resource-cleanup-skipped",
                        message=(
                            "Unused page-resource pruning was skipped because qpdf could "
                            "not analyze this document's resource usage safely"
                        ),
                        severity=WarningSeverity.INFO,
                    )
                )
            staging = context.workspace / "compressed.pdf"
            source.save(
                staging,
                compress_streams=True,
                stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                recompress_flate=True,
                deterministic_id=True,
            )

        input_bytes = artifacts[0].size_bytes
        output_bytes = staging.stat().st_size
        reduction_percent = (
            round((1 - output_bytes / input_bytes) * 100, 2) if input_bytes else 0.0
        )
        if output_bytes >= input_bytes:
            fidelity.append(
                FidelityWarning(
                    code="compress-no-reduction",
                    message=(
                        f"Lossless optimization did not shrink this file "
                        f"({input_bytes:,} → {output_bytes:,} bytes); it was already "
                        "tightly compressed"
                    ),
                    severity=WarningSeverity.INFO,
                )
            )

        context.emit("compare", message="comparing rendered pages against the source")
        comparison = compare_page_renders(
            source_path,
            staging,
            total,
            source_password=options.password,
            context=context,
        )
        if not comparison["identical"]:
            raise PipelineError(
                "Compressed output rendered differently from the source on page(s) "
                f"{comparison['mismatched_pages']}; nothing was published"
            )

        return ExecuteResult(
            candidates=[
                CandidateOutput(
                    workspace_path=staging,
                    destination=output,
                    expected_pages=total,
                    render_all=True,
                )
            ],
            fidelity_warnings=fidelity,
            security_warnings=security,
            output_page_count=total,
            details={
                "preset": preset,
                "compression": {
                    "input_bytes": input_bytes,
                    "output_bytes": output_bytes,
                    "bytes_saved": input_bytes - output_bytes,
                    "reduction_percent": reduction_percent,
                    "images_recompressed": 0,
                },
                "render_compare": comparison,
            },
        )

    return run_pipeline(
        operation="compress",
        input_paths=[input_path],
        execute=execute,
        engine_name=engine_name,
        engine_version=engine_version,
        collision=options.collision,
        settings=options.settings,
        progress=options.progress,
    )
