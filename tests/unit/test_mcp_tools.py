"""Registry drift and shared-schema coverage for MCP tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from localdocforge.config.settings import Settings
from localdocforge.domain.models import ConversionReport, ReportStatus
from localdocforge.engines.registry import CAPABILITY_SPECS
from localdocforge.mcp import executor as executor_module
from localdocforge.mcp import tools as tool_module


def test_tool_list_and_schemas_are_generated_from_implemented_registry() -> None:
    bindings = tool_module.tool_bindings()
    definitions = tool_module.tool_definitions()
    expected = [spec for spec in CAPABILITY_SPECS if spec.implemented]

    assert [binding.spec for binding in bindings] == expected
    assert [tool.name for tool in definitions] == [spec.id for spec in expected]
    assert [tool.inputSchema for tool in definitions] == [
        binding.model.model_json_schema(mode="validation") for binding in bindings
    ]
    assert all(tool.inputSchema["additionalProperties"] is False for tool in definitions)
    assert all(
        tool.annotations is not None
        and tool.annotations.destructiveHint is (tool.name != "inspect")
        for tool in definitions
    )
    assert all(
        tool.inputSchema["properties"]["strict_fidelity"]["default"] is False
        and "strict_fidelity" not in tool.inputSchema.get("required", [])
        for tool in definitions
    )
    assert all(
        "complete" in tool.inputSchema["properties"]["strict_fidelity"]["description"]
        and "no-known-loss" in tool.inputSchema["properties"]["strict_fidelity"]["description"]
        and "publication" in tool.inputSchema["properties"]["strict_fidelity"]["description"]
        for tool in definitions
    )


def test_mcp_strict_fidelity_argument_uses_shared_strict_boolean_model(
    tmp_path: Path,
) -> None:
    base = {"input": str(tmp_path / "input.pdf")}
    assert (
        tool_module.validate_tool_arguments(
            "inspect", {**base, "strict_fidelity": True}
        ).strict_fidelity
        is True
    )
    assert (
        tool_module.validate_tool_arguments(
            "inspect", {**base, "strict_fidelity": "false"}
        ).strict_fidelity
        is False
    )

    for invalid in (1, "yes", None):
        with pytest.raises(
            tool_module.ToolArgumentsError,
            match="strict_fidelity: 'strict_fidelity' must be true or false",
        ):
            tool_module.validate_tool_arguments("inspect", {**base, "strict_fidelity": invalid})


def test_missing_implemented_operation_binding_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = dict(tool_module.MCP_OPERATION_MODELS)
    models.pop("merge")
    monkeypatch.setattr(tool_module, "MCP_OPERATION_MODELS", models)

    with pytest.raises(tool_module.ToolRegistryError, match="merge.*parameter model"):
        tool_module.tool_bindings()


def test_unknown_and_unimplemented_tools_are_distinct_refusals() -> None:
    with pytest.raises(tool_module.ToolLookupError, match="Unknown tool"):
        tool_module.resolve_tool("does-not-exist")
    with pytest.raises(tool_module.ToolLookupError, match="not implemented"):
        tool_module.resolve_tool("sign")


def test_absolute_paths_are_normalized_and_relative_paths_are_rejected(
    tmp_path: Path,
) -> None:
    normalized = tmp_path / "normalized.pdf"
    lexical = tmp_path / "nested" / ".." / normalized.name
    parsed = tool_module.validate_tool_arguments(
        "merge",
        {
            "inputs": [str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")],
            "output": str(lexical),
        },
    )
    assert isinstance(parsed, tool_module.MergeToolParams)
    assert parsed.output == normalized.resolve(strict=False)

    with pytest.raises(tool_module.ToolArgumentsError, match="path must be absolute"):
        tool_module.validate_tool_arguments(
            "merge",
            {
                "inputs": ["relative-a.pdf", "relative-b.pdf"],
                "output": "relative-output.pdf",
            },
        )


def test_path_normalization_is_lexical_before_worker_containment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_resolve(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("parent-side path validation must not touch the filesystem")

    monkeypatch.setattr(Path, "resolve", forbidden_resolve)
    parsed = tool_module.validate_tool_arguments(
        "inspect",
        {"input": str(Path.cwd() / "nested" / ".." / "input.pdf")},
    )
    assert parsed.input == Path.cwd() / "input.pdf"


def test_validation_errors_never_echo_password_values(tmp_path: Path) -> None:
    secret = "mcp-password-sentinel-DO-NOT-LEAK"
    with pytest.raises(tool_module.ToolArgumentsError) as caught:
        tool_module.validate_tool_arguments(
            "merge",
            {
                "inputs": [],
                "output": str(tmp_path / "out.pdf"),
                "password": secret,
            },
        )
    assert secret not in str(caught.value)

    with pytest.raises(tool_module.ToolArgumentsError) as page_error:
        tool_module.validate_tool_arguments(
            "merge",
            {
                "inputs": [str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")],
                "output": str(tmp_path / "out.pdf"),
                "pages": [secret, None],
                "password": secret,
            },
        )
    assert secret not in str(page_error.value)

    with pytest.raises(tool_module.ToolArgumentsError) as extra_key_error:
        tool_module.validate_tool_arguments(
            "inspect",
            {
                "input": str(tmp_path / "input.pdf"),
                secret: "value",
            },
        )
    assert secret not in str(extra_key_error.value)

    with pytest.raises(tool_module.ToolLookupError) as unknown_error:
        tool_module.resolve_tool(secret)
    assert secret not in str(unknown_error.value)


def test_input_list_bound_is_exposed_in_json_schema() -> None:
    definitions = {tool.name: tool for tool in tool_module.tool_definitions()}
    assert definitions["merge"].inputSchema["properties"]["inputs"]["maxItems"] == 256
    assert definitions["convert-images"].inputSchema["properties"]["inputs"]["maxItems"] == 256


def test_registry_generated_ocr_binding_exposes_path_and_collision_contract() -> None:
    ocr_spec = next(
        spec for spec in CAPABILITY_SPECS if spec.implemented and spec.operation == "ocr"
    )
    binding = next(binding for binding in tool_module.tool_bindings() if binding.spec is ocr_spec)
    definition = next(
        tool for tool in tool_module.tool_definitions() if tool.name == binding.spec.id
    )

    assert binding.model is tool_module.OcrToolParams
    properties = definition.inputSchema["properties"]
    assert {"input", "output"} <= set(definition.inputSchema["required"])
    assert "sidecar" not in definition.inputSchema["required"]
    assert "collision" not in definition.inputSchema["required"]
    assert properties["input"]["type"] == "string"
    assert properties["input"]["format"] == "path"
    assert properties["input"]["description"].startswith("Absolute ")
    assert properties["output"]["type"] == "string"
    assert properties["output"]["format"] == "path"
    assert properties["output"]["description"].startswith("Absolute ")
    assert properties["sidecar"]["default"] is None
    assert properties["sidecar"]["description"].startswith("Optional absolute ")
    assert {variant.get("type") for variant in properties["sidecar"]["anyOf"]} == {
        "string",
        "null",
    }
    sidecar_path = next(
        variant for variant in properties["sidecar"]["anyOf"] if variant.get("type") == "string"
    )
    assert sidecar_path["format"] == "path"
    assert properties["collision"]["default"] == "fail"
    assert definition.inputSchema["$defs"]["CollisionPolicy"]["enum"] == [
        "fail",
        "rename",
        "overwrite",
    ]

    for field in ("input", "output", "sidecar"):
        arguments: dict[str, Any] = {
            "input": str(Path.cwd() / "input.pdf"),
            "output": str(Path.cwd() / "output.pdf"),
            "sidecar": str(Path.cwd() / "output.txt"),
        }
        arguments[field] = f"relative-{field}"
        with pytest.raises(tool_module.ToolArgumentsError, match="path must be absolute"):
            tool_module.validate_tool_arguments(binding.spec.id, arguments)


@pytest.mark.parametrize(("configured", "requested"), [(False, True), (True, False)])
def test_ocr_executor_forwards_validated_transport_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
    requested: bool,
) -> None:
    observed: dict[str, Any] = {}
    report = ConversionReport(
        operation="ocr",
        status=ReportStatus.SUCCESS,
        job_id="synthetic-mcp-ocr",
    )

    def fake_ocr(input_file: Path, output: Path, *, options: Any) -> ConversionReport:
        observed.update(input=input_file, output=output, options=options)
        return report

    monkeypatch.setattr(executor_module.ocr_ops, "ocr_pdf", fake_ocr)
    input_file = tmp_path / "input.pdf"
    output = tmp_path / "output.pdf"
    sidecar = tmp_path / "output.txt"

    result = executor_module.execute_tool(
        "ocr",
        {
            "input": str(input_file),
            "output": str(output),
            "sidecar": str(sidecar),
            "language": "eng+deu",
            "skip_text": True,
            "force_ocr": False,
            "strict_fidelity": requested,
            "collision": "rename",
            "password": "synthetic-password",
        },
        Settings(jobs_root=tmp_path / "jobs", strict_fidelity=configured),
        inspection_output=tmp_path / "inspection.json",
    )

    assert result.report is report
    assert observed["input"] == input_file
    assert observed["output"] == output
    options = observed["options"]
    assert options.language == "eng+deu"
    assert options.sidecar == sidecar
    assert options.skip_text is True
    assert options.force_ocr is False
    assert options.settings.strict_fidelity is (configured or requested)
    assert options.collision.value == "rename"
    assert options.password == "synthetic-password"
