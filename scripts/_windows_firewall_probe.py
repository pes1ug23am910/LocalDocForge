#!/usr/bin/env python3
"""Local-only socket probe for the opt-in Windows Firewall release gate.

The probe never contacts another system. It starts TCP listeners on loopback
and on an IPv4 address assigned to this host, then connects back to them. The
PowerShell gate runs it once before installing its rule and once while the
rule is active.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProbeResult:
    mode: str
    loopback: str
    non_loopback: str
    non_loopback_error: str | None
    dns: str


class ConnectionAttemptError(RuntimeError):
    """The listener was ready, but its matching client could not connect."""


def _validated_non_loopback_address(value: str) -> str:
    address = ipaddress.ip_address(value)
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError("the synthetic firewall probe currently requires an IPv4 address")
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError("the address must be a non-loopback unicast IPv4 address")
    return str(address)


def _roundtrip(address: str, timeout: float = 2.0) -> None:
    """Exchange a fixed marker through a listener on this machine."""

    marker = b"localdocforge-local-firewall-probe"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.settimeout(timeout)
        listener.bind((address, 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            try:
                client.connect((address, port))
            except OSError as error:
                raise ConnectionAttemptError(
                    f"connect to local address failed: {type(error).__name__}: {error}"
                ) from error
            client.sendall(marker)

        connection, _ = listener.accept()
        with connection:
            connection.settimeout(timeout)
            received = connection.recv(len(marker) + 1)
    if received != marker:
        raise RuntimeError("local socket probe received an unexpected payload")


def run_probe(mode: str, non_loopback_address: str) -> ProbeResult:
    address = _validated_non_loopback_address(non_loopback_address)
    _roundtrip("127.0.0.1")

    if mode == "baseline":
        _roundtrip(address)
        non_loopback = "allowed"
        non_loopback_error = None
    elif mode == "enforced":
        try:
            _roundtrip(address)
        except ConnectionAttemptError as error:
            non_loopback = "denied"
            non_loopback_error = str(error)
        else:
            raise RuntimeError(
                "the exact-program firewall rule did not deny the local "
                "non-loopback TCP connection"
            )
    else:  # Defensive for callers that bypass argparse.
        raise ValueError(f"unknown probe mode: {mode}")

    return ProbeResult(
        mode=mode,
        loopback="allowed",
        non_loopback=non_loopback,
        non_loopback_error=non_loopback_error,
        dns=(
            "not-tested: Windows DNS Client can mediate getaddrinfo, so an "
            "exact-program firewall rule is not reliable DNS-denial proof"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("baseline", "enforced"), required=True)
    parser.add_argument("--non-loopback-address", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(asdict(run_probe(args.mode, args.non_loopback_address)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
