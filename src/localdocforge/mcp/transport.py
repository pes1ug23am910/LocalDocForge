"""Bounded, UTF-8-only MCP stdio transport with a private protocol handle.

The official MCP 1.x transport supplies the protocol/session machinery but it
does not bound stdio frames and it leaves ordinary process stdout attached to
the protocol pipe.  This transport keeps the SDK's ``SessionMessage`` streams
while adding the two LocalDocForge requirements that matter most for a local
document worker: bounded hostile input and byte-level stdout purity.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO, cast

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.shared.message import SessionMessage

MAX_STDIO_FRAME_BYTES = 1024 * 1024
MAX_JSON_NESTING = 64
_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class _RawFrame:
    payload: dict[str, Any]


class _DuplicateKey(ValueError):
    pass


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is forbidden")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _nesting_exceeds_limit(data: bytes, limit: int = MAX_JSON_NESTING) -> bool:
    """Count JSON container depth without interpreting string contents."""
    depth = 0
    in_string = False
    escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            if depth > limit:
                return True
        elif byte in (0x7D, 0x5D):  # } ]
            depth = max(0, depth - 1)
    return False


def _error_frame(code: int, message: str) -> _RawFrame:
    return _RawFrame(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": code, "message": message},
        }
    )


@contextlib.contextmanager
def _private_protocol_stdout() -> Iterator[BinaryIO]:
    """Duplicate the wire, then redirect inherited stdout to stderr.

    ``os.dup`` returns non-inheritable descriptors on supported Python
    versions.  On Windows the Win32 standard-handle slot is redirected too,
    so spawned native children cannot inherit the MCP wire as ordinary stdout.
    """
    stdout_fd = sys.stdout.fileno()
    stderr_fd = sys.stderr.fileno()
    protocol_fd = os.dup(stdout_fd)
    restore_fd = os.dup(stdout_fd)
    os.set_inheritable(protocol_fd, False)
    os.set_inheritable(restore_fd, False)
    original_windows_handle: object | None = None
    try:
        os.dup2(stderr_fd, stdout_fd)
        if os.name == "nt":
            import win32api  # type: ignore[import-untyped]

            original_windows_handle = win32api.GetStdHandle(win32api.STD_OUTPUT_HANDLE)
            stderr_handle = win32api.GetStdHandle(win32api.STD_ERROR_HANDLE)
            win32api.SetStdHandle(win32api.STD_OUTPUT_HANDLE, stderr_handle)
        with os.fdopen(protocol_fd, "wb", buffering=0, closefd=True) as protocol:
            protocol_fd = -1
            yield protocol
    finally:
        with contextlib.suppress(OSError):
            os.dup2(restore_fd, stdout_fd)
        if os.name == "nt" and original_windows_handle is not None:
            with contextlib.suppress(Exception):
                import win32api  # type: ignore[import-untyped]

                win32api.SetStdHandle(win32api.STD_OUTPUT_HANDLE, original_windows_handle)
        with contextlib.suppress(OSError):
            os.close(restore_fd)
        if protocol_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(protocol_fd)


async def _decode_message(
    line: bytes,
    send_read: MemoryObjectSendStream[SessionMessage | Exception],
    send_write: MemoryObjectSendStream[object],
) -> None:
    if len(line) > MAX_STDIO_FRAME_BYTES:
        await send_write.send(_error_frame(types.INVALID_REQUEST, "JSON-RPC frame is too large"))
        return
    if _nesting_exceeds_limit(line):
        await send_write.send(
            _error_frame(types.INVALID_REQUEST, "JSON-RPC nesting limit exceeded")
        )
        return
    try:
        text = line.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        await send_write.send(_error_frame(types.PARSE_ERROR, "Parse error"))
        return
    if not isinstance(payload, dict):
        await send_write.send(_error_frame(types.INVALID_REQUEST, "Invalid Request"))
        return
    try:
        message = types.JSONRPCMessage.model_validate(payload)
    except Exception:
        await send_write.send(_error_frame(types.INVALID_REQUEST, "Invalid Request"))
        return
    await send_read.send(SessionMessage(message))


@contextlib.asynccontextmanager
async def bounded_stdio_server() -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Yield MCP SDK streams backed by bounded binary stdin/stdout."""
    read_send, read_receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_send_raw, write_receive = anyio.create_memory_object_stream[object](0)
    write_send = cast(MemoryObjectSendStream[SessionMessage], write_send_raw)

    with _private_protocol_stdout() as protocol_binary:
        stdin = anyio.wrap_file(sys.stdin.buffer)
        stdout = anyio.wrap_file(protocol_binary)

        async def stdin_reader() -> None:
            buffer = bytearray()
            discarding_oversize = False
            async with read_send:
                while True:
                    chunk = await stdin.read1(_READ_CHUNK_BYTES)
                    if not chunk:
                        if buffer and not discarding_oversize:
                            await _decode_message(bytes(buffer), read_send, write_send_raw)
                        return
                    buffer.extend(chunk)
                    while True:
                        newline = buffer.find(b"\n")
                        if newline < 0:
                            if len(buffer) > MAX_STDIO_FRAME_BYTES:
                                buffer.clear()
                                discarding_oversize = True
                            break
                        line = bytes(buffer[:newline])
                        del buffer[: newline + 1]
                        if discarding_oversize:
                            discarding_oversize = False
                            await write_send_raw.send(
                                _error_frame(types.INVALID_REQUEST, "JSON-RPC frame is too large")
                            )
                            continue
                        await _decode_message(line, read_send, write_send_raw)

        async def stdout_writer() -> None:
            async with write_receive:
                async for item in write_receive:
                    if isinstance(item, _RawFrame):
                        encoded = json.dumps(
                            item.payload,
                            ensure_ascii=True,
                            allow_nan=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    else:
                        session_message = cast(SessionMessage, item)
                        encoded = session_message.message.model_dump_json(
                            by_alias=True,
                            exclude_none=True,
                        ).encode("utf-8")
                    await stdout.write(encoded + b"\n")
                    await stdout.flush()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(stdin_reader)
            task_group.start_soon(stdout_writer)
            yield read_receive, write_send
