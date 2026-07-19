"""Write ConversionReports to disk in machine- and human-readable form."""

from __future__ import annotations

from pathlib import Path

from localdocforge.domain.models import ConversionReport


def write_report_files(
    report: ConversionReport, directory: Path, basename: str
) -> tuple[Path, Path]:
    """Write ``<basename>.report.json`` and ``.report.txt`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{basename}.report.json"
    text_path = directory / f"{basename}.report.txt"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    text_path.write_text(report.to_human() + "\n", encoding="utf-8")
    return json_path, text_path
