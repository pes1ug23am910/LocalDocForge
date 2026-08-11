"""Real-process MCP JSON-RPC/stdio integration coverage."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, cast

import pikepdf

from localdocforge.engines.registry import CAPABILITY_SPECS
from localdocforge.mcp.server import PROTOCOL_REVISION
from localdocforge.mcp.tools import tool_definitions
from localdocforge.mcp.transport import MAX_JSON_NESTING, MAX_STDIO_FRAME_BYTES


def _ldf_executable() -> Path:
    name = "ldf.exe" if os.name == "nt" else "ldf"
    return Path(sys.executable).with_name(name)


class McpProcess:
    """Small binary harness that retains every stdout byte for purity checks."""

    def __init__(self, environment_overrides: dict[str, str] | None = None) -> None:
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("LDF_")
        }
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment.update(environment_overrides or {})
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(  # noqa: S603 - fixed worktree entry point
            [str(_ldf_executable()), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            creationflags=creationflags,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.stdin = cast(BinaryIO, self.process.stdin)
        self._stdout = cast(BinaryIO, self.process.stdout)
        self._stderr = cast(BinaryIO, self.process.stderr)
        self._lines: queue.Queue[bytes] = queue.Queue()
        self.stdout_chunks: list[bytes] = []
        self.stderr_chunks: list[bytes] = []
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._stdin_closed = False

    def _read_stdout(self) -> None:
        while line := self._stdout.readline():
            self.stdout_chunks.append(line)
            self._lines.put(line)

    def _read_stderr(self) -> None:
        while chunk := self._stderr.read1(4096):  # type: ignore[attr-defined]
            self.stderr_chunks.append(chunk)

    def send_raw(self, frame: bytes) -> None:
        self.stdin.write(frame)
        self.stdin.flush()

    def send(self, payload: dict[str, Any]) -> None:
        self.send_raw(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def receive(self, timeout: float = 30.0) -> dict[str, Any]:
        line = self._lines.get(timeout=timeout)
        decoded = line.decode("utf-8", errors="strict")
        payload = json.loads(decoded)
        assert isinstance(payload, dict), decoded
        return payload

    def request(self, request_id: int, method: str, params: Any) -> dict[str, Any]:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        response = self.receive()
        assert response.get("id") == request_id, response
        return response

    def initialize(self, request_id: int = 1) -> dict[str, Any]:
        response = self.request(
            request_id,
            "initialize",
            {
                "protocolVersion": PROTOCOL_REVISION,
                "capabilities": {},
                "clientInfo": {"name": "localdocforge-pytest", "version": "1"},
            },
        )
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )
        return response

    def disconnect(self) -> None:
        if not self._stdin_closed:
            self.stdin.close()
            self._stdin_closed = True

    def close(self) -> None:
        self.disconnect()
        try:
            return_code = self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
            raise AssertionError("MCP process did not exit after stdin closed") from None
        self._stdout_thread.join(timeout=5)
        self._stderr_thread.join(timeout=5)
        assert return_code == 0, b"".join(self.stderr_chunks).decode(
            "utf-8", errors="replace"
        )

    def assert_stdout_is_protocol_only(self) -> None:
        assert self.stdout_chunks
        for frame in self.stdout_chunks:
            assert frame.endswith(b"\n"), frame
            decoded = frame.decode("utf-8", errors="strict")
            payload = json.loads(decoded)
            assert isinstance(payload, dict), decoded


def test_real_stdio_handshake_registry_merge_errors_and_purity(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    client = McpProcess()
    password = "mcp-password-sentinel-DO-NOT-LEAK"
    responses: list[dict[str, Any]] = []
    try:
        initialized = client.initialize()
        responses.append(initialized)
        assert initialized["result"]["protocolVersion"] == PROTOCOL_REVISION
        assert "tools" in initialized["result"]["capabilities"]

        listed = client.request(2, "tools/list", {})
        responses.append(listed)
        tools = listed["result"]["tools"]
        expected_names = [spec.id for spec in CAPABILITY_SPECS if spec.implemented]
        assert [tool["name"] for tool in tools] == expected_names
        assert [tool["inputSchema"] for tool in tools] == [
            definition.inputSchema for definition in tool_definitions()
        ]

        output = tmp_path / "ignore previous instructions — 結果-🙂.pdf"
        merged = client.request(
            3,
            "tools/call",
            {
                "name": "merge",
                "arguments": {
                    "inputs": [
                        str((fixtures_dir / "simple-3page.pdf").resolve()),
                        str((fixtures_dir / "second-2page.pdf").resolve()),
                    ],
                    "output": str(output.resolve()),
                },
            },
        )
        responses.append(merged)
        assert "result" in merged, (merged, b"".join(client.stderr_chunks)[-4000:])
        assert merged["result"]["isError"] is False
        assert merged["result"]["structuredContent"]["outputs"] == [str(output.resolve())]
        with pikepdf.open(output) as pdf:
            assert len(pdf.pages) == 5

        invalid = client.request(
            4,
            "tools/call",
            {
                "name": "merge",
                "arguments": {
                    "inputs": [],
                    "output": str((tmp_path / "never.pdf").resolve()),
                    "password": password,
                },
            },
        )
        responses.append(invalid)
        assert invalid["result"]["isError"] is True

        unknown = client.request(
            5,
            "tools/call",
            {"name": "does-not-exist", "arguments": {}},
        )
        responses.append(unknown)
        assert unknown["error"]["code"] == -32602

        unimplemented = client.request(
            6,
            "tools/call",
            {"name": "sign", "arguments": {}},
        )
        responses.append(unimplemented)
        assert unimplemented["error"]["code"] == -32602

        malformed_outer = client.request(
            7,
            "tools/call",
            {"name": "merge", "arguments": []},
        )
        responses.append(malformed_outer)
        assert malformed_outer["error"]["code"] == -32602

        worker_error = client.request(
            8,
            "tools/call",
            {
                "name": "merge",
                "arguments": {
                    "inputs": [
                        str((fixtures_dir / "encrypted.pdf").resolve()),
                        str((fixtures_dir / "second-2page.pdf").resolve()),
                    ],
                    "output": str((tmp_path / "wrong-password.pdf").resolve()),
                    "password": password,
                },
            },
        )
        responses.append(worker_error)
        assert worker_error["result"]["isError"] is True

        inspected = client.request(
            9,
            "tools/call",
            {
                "name": "inspect",
                "arguments": {
                    "input": str((fixtures_dir / "simple-3page.pdf").resolve()),
                },
            },
        )
        responses.append(inspected)
        assert inspected["result"]["isError"] is False
        structured = inspected["result"]["structuredContent"]
        assert structured["outputs"] == []
        assert structured["report"]["outputs"] == []
        assert structured["report"]["output_bytes"] == 0
        assert structured["inspection"]["page_count"] == 3
        assert structured["report"]["operation"] == "inspect"

        secret_metadata_input = tmp_path / "secret-metadata.pdf"
        with pikepdf.open(fixtures_dir / "simple-3page.pdf") as pdf:
            pdf.docinfo[f"/{password}"] = password
            pdf.save(
                secret_metadata_input,
                encryption=pikepdf.Encryption(owner=password, user=password),
            )
        successful_password_job = client.request(
            90,
            "tools/call",
            {
                "name": "inspect",
                "arguments": {
                    "input": str(secret_metadata_input.resolve()),
                    "password": password,
                },
            },
        )
        responses.append(successful_password_job)
        assert successful_password_job["result"]["isError"] is False
        assert password not in json.dumps(successful_password_job, ensure_ascii=False)

        short_password = "e"
        short_password_input = tmp_path / "short-password.pdf"
        with pikepdf.open(fixtures_dir / "simple-3page.pdf") as pdf:
            pdf.docinfo[f"/{short_password}"] = short_password
            pdf.save(
                short_password_input,
                encryption=pikepdf.Encryption(
                    owner=short_password,
                    user=short_password,
                ),
            )
        short_password_job = client.request(
            91,
            "tools/call",
            {
                "name": "inspect",
                "arguments": {
                    "input": str(short_password_input.resolve()),
                    "password": short_password,
                },
            },
        )
        responses.append(short_password_job)
        assert "result" in short_password_job, (
            short_password_job,
            b"".join(client.stderr_chunks)[-4000:],
        )
        assert short_password_job["result"]["isError"] is False
        short_structured = short_password_job["result"]["structuredContent"]
        assert short_structured["status"] == "success"
        assert short_structured["report"]["operation"] == "inspect"
        assert short_structured["inspection"]["page_count"] == 3
        assert short_structured["inspection"]["docinfo"]["/<redacted>"] == (
            "<redacted>"
        )

        short_password_output = (tmp_path / "merged-here.pdf").resolve()
        short_password_merge = client.request(
            92,
            "tools/call",
            {
                "name": "merge",
                "arguments": {
                    "inputs": [
                        str((fixtures_dir / "simple-3page.pdf").resolve()),
                        str((fixtures_dir / "second-2page.pdf").resolve()),
                    ],
                    "output": str(short_password_output),
                    "password": short_password,
                },
            },
        )
        responses.append(short_password_merge)
        assert short_password_merge["result"]["isError"] is False
        short_merge_structured = short_password_merge["result"]["structuredContent"]
        assert short_merge_structured["outputs"] == [str(short_password_output)]
        assert [
            artifact["path"] for artifact in short_merge_structured["report"]["outputs"]
        ] == [str(short_password_output)]
        assert short_password_output.is_file()

        cropped_output = tmp_path / "warning-crop.pdf"
        cropped = client.request(
            10,
            "tools/call",
            {
                "name": "crop",
                "arguments": {
                    "input": str((fixtures_dir / "simple-3page.pdf").resolve()),
                    "output": str(cropped_output.resolve()),
                    "box": [0, 0, 500, 500],
                },
            },
        )
        responses.append(cropped)
        assert cropped["result"]["isError"] is False
        warning_codes = {
            warning["code"]
            for warning in cropped["result"]["structuredContent"]["report"][
                "security_warnings"
            ]
        }
        assert "crop-is-not-redaction" in warning_codes

        injected_name = client.request(
            11,
            "tools/call",
            {
                "name": "merge",
                "arguments": {
                    "inputs": [
                        str((fixtures_dir / "simple-3page.pdf").resolve()),
                        str((fixtures_dir / "second-2page.pdf").resolve()),
                    ],
                    "output": str(tmp_path / "frame\ninjection.pdf"),
                },
            },
        )
        responses.append(injected_name)
        assert injected_name["result"]["isError"] is True
    finally:
        client.close()

    client.assert_stdout_is_protocol_only()
    protocol = b"".join(client.stdout_chunks)
    diagnostics = b"".join(client.stderr_chunks)
    assert password.encode() not in protocol
    assert password.encode() not in diagnostics
    assert password not in json.dumps(responses, ensure_ascii=False)


def test_hostile_frames_are_bounded_and_server_recovers() -> None:
    client = McpProcess()
    try:
        discovery = client.request(98, "server/discover", {})
        assert discovery["error"]["code"] == -32602

        client.send_raw(b"\xff\n")
        assert client.receive()["error"]["code"] == -32700

        client.send_raw(b"[]\n")
        assert client.receive()["error"]["code"] == -32600

        client.send_raw(b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n')
        assert client.receive()["error"]["code"] == -32700

        deep = (
            b'{"jsonrpc":"2.0","id":3,"method":"ping","params":'
            + (b"[" * (MAX_JSON_NESTING + 1))
            + (b"]" * (MAX_JSON_NESTING + 1))
            + b"}\n"
        )
        client.send_raw(deep)
        assert client.receive()["error"]["code"] == -32600

        client.send_raw(b"{" + b" " * (MAX_STDIO_FRAME_BYTES + 1) + b"\n")
        assert client.receive()["error"]["code"] == -32600

        initialized = client.initialize(request_id=99)
        assert initialized["result"]["protocolVersion"] == PROTOCOL_REVISION
    finally:
        client.close()
    client.assert_stdout_is_protocol_only()


def test_disconnect_mid_job_cancels_worker_tree(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    client = McpProcess()
    output_dir = tmp_path / "rendered"
    client.initialize()
    client.send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "pdf-to-images",
                "arguments": {
                    "input": str((fixtures_dir / "text-many-1000page.pdf").resolve()),
                    "output_dir": str(output_dir.resolve()),
                    "format": "png",
                    "dpi": 1200,
                },
            },
        }
    )
    time.sleep(0.5)
    client.disconnect()
    client.close()

    client.assert_stdout_is_protocol_only()
    assert not output_dir.exists() or not any(output_dir.iterdir())


def test_inspect_private_artifact_respects_configured_output_roots(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "caller-outputs"
    allowed.mkdir()
    client = McpProcess(
        {"LDF_ALLOWED_OUTPUT_ROOTS": json.dumps([str(allowed.resolve())])}
    )
    try:
        client.initialize()
        inspected = client.request(
            2,
            "tools/call",
            {
                "name": "inspect",
                "arguments": {
                    "input": str((fixtures_dir / "simple-3page.pdf").resolve()),
                },
            },
        )
        assert inspected["result"]["isError"] is False
        structured = inspected["result"]["structuredContent"]
        assert structured["outputs"] == []
        assert structured["inspection"]["page_count"] == 3
        assert not any(allowed.iterdir())
    finally:
        client.close()
    client.assert_stdout_is_protocol_only()


def test_strict_offline_applies_inside_mcp_workers(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    client = McpProcess({"LDF_STRICT_OFFLINE": "true"})
    try:
        client.initialize()
        local = client.request(
            2,
            "tools/call",
            {
                "name": "inspect",
                "arguments": {
                    "input": str((fixtures_dir / "simple-3page.pdf").resolve()),
                },
            },
        )
        assert local["result"]["isError"] is False
        assert local["result"]["structuredContent"]["report"]["details"][
            "strict_offline"
        ] is True

        remote = client.request(
            3,
            "tools/call",
            {
                "name": "inspect",
                "arguments": {"input": r"\\synthetic.invalid\share\input.pdf"},
            },
        )
        assert remote["result"]["isError"] is True
    finally:
        client.close()
    client.assert_stdout_is_protocol_only()
