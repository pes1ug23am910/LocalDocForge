"""Python startup guard used only by scripts/run_blocked_network.py."""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

if os.environ.get("LDF_BLOCK_NETWORK") == "1":
    _original_create_connection = socket.create_connection
    _original_getaddrinfo = socket.getaddrinfo
    _original_gethostbyaddr = socket.gethostbyaddr
    _original_gethostbyname = socket.gethostbyname
    _original_gethostbyname_ex = socket.gethostbyname_ex
    _original_bind = socket.socket.bind
    _original_connect = socket.socket.connect
    _original_connect_ex = socket.socket.connect_ex
    _original_sendto = socket.socket.sendto

    def _loopback_host(host: Any) -> bool:
        if host is None:
            return True
        if isinstance(host, bytes):
            try:
                host = host.decode("ascii")
            except UnicodeDecodeError:
                return False
        if not isinstance(host, str):
            return False
        normalized = host.strip().lower()
        if normalized == "localhost":
            return True
        if normalized.startswith("[") and normalized.endswith("]"):
            normalized = normalized[1:-1]
        normalized = normalized.split("%", 1)[0]
        try:
            return ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            return False

    def _internet_address_allowed(address: Any) -> bool:
        return isinstance(address, tuple) and bool(address) and _loopback_host(address[0])

    def _deny(kind: str, target: Any) -> OSError:
        return OSError(f"blocked-network gate denied {kind}: {target!r}")

    def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _loopback_host(host):
            raise _deny("DNS resolution", host)
        return _original_getaddrinfo(host, *args, **kwargs)

    def _guarded_gethostbyname(host: str) -> str:
        if not _loopback_host(host):
            raise _deny("DNS resolution", host)
        return _original_gethostbyname(host)

    def _guarded_gethostbyname_ex(host: str) -> Any:
        if not _loopback_host(host):
            raise _deny("DNS resolution", host)
        return _original_gethostbyname_ex(host)

    def _guarded_gethostbyaddr(host: str) -> Any:
        if not _loopback_host(host):
            raise _deny("reverse DNS resolution", host)
        return _original_gethostbyaddr(host)

    def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _internet_address_allowed(address):
            raise _deny("connection", address)
        return _original_create_connection(address, *args, **kwargs)

    def _guarded_connect(instance: socket.socket, address: Any) -> Any:
        if instance.family in (socket.AF_INET, socket.AF_INET6):
            if not _internet_address_allowed(address):
                raise _deny("connection", address)
        return _original_connect(instance, address)

    def _guarded_connect_ex(instance: socket.socket, address: Any) -> int:
        if instance.family in (socket.AF_INET, socket.AF_INET6):
            if not _internet_address_allowed(address):
                raise _deny("connection", address)
        return _original_connect_ex(instance, address)

    def _guarded_bind(instance: socket.socket, address: Any) -> Any:
        if instance.family in (socket.AF_INET, socket.AF_INET6):
            if not _internet_address_allowed(address):
                raise _deny("bind", address)
        return _original_bind(instance, address)

    def _guarded_sendto(instance: socket.socket, *args: Any) -> int:
        address = args[-1] if args else None
        if instance.family in (socket.AF_INET, socket.AF_INET6):
            if not _internet_address_allowed(address):
                raise _deny("datagram", address)
        return _original_sendto(instance, *args)

    socket.getaddrinfo = _guarded_getaddrinfo
    socket.gethostbyname = _guarded_gethostbyname
    socket.gethostbyname_ex = _guarded_gethostbyname_ex
    socket.gethostbyaddr = _guarded_gethostbyaddr
    socket.create_connection = _guarded_create_connection
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.socket.bind = _guarded_bind
    socket.socket.sendto = _guarded_sendto
