"""Static guardrails for the in-tree fidelity-warning catalog."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src" / "localdocforge"

EXPECTED_DIRECT_CLASSIFICATIONS = {
    "image-fit-downscaled": ("STRUCTURAL", "KNOWN_LOSS"),
    "image-aspect-distorted": ("STRUCTURAL", "KNOWN_LOSS"),
    "image-downscaled": ("STRUCTURAL", "KNOWN_LOSS"),
    "images-reencoded": ("DECLARED", "KNOWN_LOSS"),
    "resource-cleanup-skipped": ("STRUCTURAL", "ADVISORY"),
    "signature-invalidated": ("STRUCTURAL", "KNOWN_LOSS"),
    "signature-presence-uncertain": ("HEURISTIC", "REVIEW"),
}


def test_every_in_tree_fidelity_warning_declares_basis_and_impact() -> None:
    missing: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "FidelityWarning":
                continue
            keywords = {keyword.arg for keyword in node.keywords if keyword.arg is not None}
            absent = {"basis", "impact"} - keywords
            if absent:
                relative = path.relative_to(ROOT)
                missing.append(f"{relative}:{node.lineno} missing {sorted(absent)}")

    assert missing == []


def test_high_consequence_direct_warning_classifications_are_stable() -> None:
    observed: dict[str, set[tuple[str, str]]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "FidelityWarning":
                continue
            keywords = {
                keyword.arg: keyword.value for keyword in node.keywords if keyword.arg is not None
            }
            code = keywords.get("code")
            basis = keywords.get("basis")
            impact = keywords.get("impact")
            if not isinstance(code, ast.Constant) or not isinstance(code.value, str):
                continue
            if code.value not in EXPECTED_DIRECT_CLASSIFICATIONS:
                continue
            if not isinstance(basis, ast.Attribute) or not isinstance(impact, ast.Attribute):
                observed.setdefault(code.value, set()).add(("<dynamic>", "<dynamic>"))
                continue
            observed.setdefault(code.value, set()).add((basis.attr, impact.attr))

    assert observed == {
        code: {classification} for code, classification in EXPECTED_DIRECT_CLASSIFICATIONS.items()
    }
