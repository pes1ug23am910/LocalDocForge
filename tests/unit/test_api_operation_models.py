"""Shared operation-model coverage for HTTP and local-agent transports."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from localdocforge.api.operations import (
    OPERATION_MODELS,
    OPERATION_PARAMS,
    OPERATIONS,
    CompressParams,
    CropParams,
    MergeParams,
    OcrHttpParams,
    PdfToMdParams,
    _ApiError,
    _effective_settings,
    parse_operation_params,
)
from localdocforge.config.settings import Settings

_EXPECTED_OPERATIONS = {
    "merge",
    "split",
    "remove-pages",
    "extract-pages",
    "organize",
    "rotate",
    "crop",
    "inspect",
    "compress",
    "ocr",
    "images-to-pdf",
    "pdf-to-images",
    "pdf-to-md",
    "md-to-pdf",
    "convert-images",
}


def _schema_variant(schema: dict[str, Any], expected_type: str) -> dict[str, Any]:
    if schema.get("type") == expected_type:
        return schema
    return next(item for item in schema["anyOf"] if item.get("type") == expected_type)


def test_operation_parameter_registry_is_derived_from_models() -> None:
    assert set(OPERATION_MODELS) == _EXPECTED_OPERATIONS
    assert set(OPERATIONS) == _EXPECTED_OPERATIONS - {"inspect"}
    assert OPERATION_PARAMS == {
        name: frozenset(model.model_fields) for name, model in OPERATION_MODELS.items()
    }
    assert all(
        model.model_json_schema().get("additionalProperties") is False
        for model in OPERATION_MODELS.values()
    )


def test_merge_accepts_native_and_legacy_pages_without_revealing_password() -> None:
    native = parse_operation_params(
        "merge",
        {"pages": ["1-2", None], "password": "highly-secret"},
    )
    legacy = parse_operation_params(
        "merge",
        {"pages": '["1-2", null]', "password": "highly-secret"},
    )

    assert isinstance(native, MergeParams)
    assert isinstance(legacy, MergeParams)
    assert native.pages == legacy.pages == ["1-2", None]
    assert "highly-secret" not in repr(native)

    pages_schema = MergeParams.model_json_schema()["properties"]["pages"]
    assert _schema_variant(pages_schema, "array")["maxItems"] == 256


def test_crop_accepts_native_array_and_legacy_comma_string() -> None:
    native = parse_operation_params("crop", {"box": [1, 2.5, 3, 4]})
    legacy = parse_operation_params("crop", {"box": "1,2.5,3,4"})

    assert isinstance(native, CropParams)
    assert isinstance(legacy, CropParams)
    assert native.box == legacy.box == (1.0, 2.5, 3.0, 4.0)

    box_schema = CropParams.model_json_schema()["properties"]["box"]
    assert box_schema["minItems"] == box_schema["maxItems"] == 4


def test_http_boolean_strings_remain_strict() -> None:
    parsed = parse_operation_params(
        "pdf-to-md",
        {"page_anchors": "false", "tables": "true"},
    )
    assert isinstance(parsed, PdfToMdParams)
    assert parsed.page_anchors is False
    assert parsed.tables is True

    with pytest.raises(_ApiError, match="'tables' must be true or false") as caught:
        parse_operation_params("pdf-to-md", {"tables": "1"})
    assert caught.value.status == 422


def test_strict_fidelity_is_shared_strict_and_optional() -> None:
    assert all(
        model.model_json_schema()["properties"]["strict_fidelity"]["default"] is False
        for model in OPERATION_MODELS.values()
    )
    assert parse_operation_params("compress", {}).strict_fidelity is False
    assert parse_operation_params(
        "compress", {"strict_fidelity": "true"}
    ).strict_fidelity is True
    assert parse_operation_params(
        "compress", {"strict_fidelity": "false"}
    ).strict_fidelity is False

    for invalid in ("1", "yes", 1, None):
        with pytest.raises(_ApiError, match="'strict_fidelity' must be true or false"):
            parse_operation_params("compress", {"strict_fidelity": invalid})


@pytest.mark.parametrize(("configured", "requested"), [(False, True), (True, False)])
def test_effective_settings_reconstruction_never_weakens_server_policy(
    configured: bool,
    requested: bool,
) -> None:
    settings = Settings(strict_fidelity=configured)
    params = CompressParams(strict_fidelity=requested)

    effective = _effective_settings(settings, params)

    assert effective is not settings
    assert settings.strict_fidelity is configured
    assert effective.strict_fidelity is (configured or requested)


def test_effective_settings_revalidates_the_complete_settings_model() -> None:
    settings = Settings(jobs_root=Path(r"\\remote-host\private-share\ldf-jobs"))
    settings.strict_offline = True

    with pytest.raises(ValidationError, match="UNC or mapped network-drive path"):
        _effective_settings(settings, CompressParams(strict_fidelity=True))


def test_shared_ocr_http_model_enforces_language_modes_and_strict_booleans() -> None:
    parsed = parse_operation_params(
        "ocr",
        {
            "language": "eng+deu",
            "sidecar": "true",
            "skip_text": "false",
            "force_ocr": False,
        },
    )

    assert isinstance(parsed, OcrHttpParams)
    assert parsed.language == "eng+deu"
    assert parsed.sidecar is True
    assert parsed.skip_text is False
    assert parsed.force_ocr is False

    invalid_params = (
        {"language": "eng;unsafe"},
        {"sidecar": "1"},
        {"skip_text": "yes"},
        {"force_ocr": 1},
        {"skip_text": "true", "force_ocr": "true"},
    )
    for params in invalid_params:
        with pytest.raises(_ApiError) as caught:
            parse_operation_params("ocr", params)
        assert caught.value.status == 422


def test_unknown_parameters_are_forbidden() -> None:
    with pytest.raises(_ApiError, match="Extra inputs are not permitted") as caught:
        parse_operation_params("inspect", {"unexpected": "value"})
    assert caught.value.status == 422


def test_worker_and_operations_import_without_fastapi() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import localdocforge.api.operations; "
                "import localdocforge.api.worker; "
                "raise SystemExit('fastapi' in sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
