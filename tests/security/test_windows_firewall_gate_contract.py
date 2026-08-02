"""Non-mutating contracts for the opt-in Windows Firewall gate."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POWERSHELL_GATE = ROOT / "scripts" / "run_windows_firewall_gate.ps1"
PROBE_PATH = ROOT / "scripts" / "_windows_firewall_probe.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("windows_firewall_probe", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves forward references through sys.modules while the
    # module body executes.
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_firewall_gate_is_exact_program_scoped_and_always_cleans_up():
    script = POWERSHELL_GATE.read_text(encoding="utf-8")

    assert script.startswith("#Requires -Version 7.0\n")
    assert "[Security.Principal.WindowsBuiltInRole]::Administrator" in script
    assert "Get-Service -Name 'MpsSvc'" in script
    assert "Get-NetFirewallProfile" in script
    assert "New-NetFirewallRule" in script
    assert "-Direction Outbound" in script
    assert "-Action Block" in script
    assert "-Program $resolvedPython" in script
    assert "-PolicyStore PersistentStore" in script
    assert "-PolicyStore ActiveStore" in script
    assert "finally {" in script
    assert "Remove-NetFirewallRule -Confirm:$false" in script
    assert "Temporary firewall rule still exists after cleanup" in script
    assert "Windows DNS Client may mediate getaddrinfo" in script
    assert "windows_addendum_network_gate = 'incomplete_dns_proof'" in script
    assert "process_exit_code = 2" in script
    assert "python_executable = $resolvedPython" not in script
    assert "python_executable_sha256 = $pythonHash" in script
    assert "host-assigned IPv4; value omitted" in script
    assert script.rstrip().endswith("exit 2")
    assert "Set-NetFirewallProfile" not in script
    assert "Disable-NetFirewallRule" not in script


def test_firewall_gate_has_valid_powershell_syntax_without_executing_it():
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable on this runner")

    command = (
        "$errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        "$env:LDF_FIREWALL_GATE_PATH, [ref]$null, [ref]$errors); "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    environment = os.environ.copy()
    environment["LDF_FIREWALL_GATE_PATH"] = str(POWERSHELL_GATE)
    completed = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr


def test_probe_rejects_loopback_as_non_loopback_target():
    probe = _load_probe()
    with pytest.raises(ValueError, match="non-loopback"):
        probe._validated_non_loopback_address("127.0.0.1")


def test_probe_baseline_and_enforced_results_are_explicit(monkeypatch):
    probe = _load_probe()
    calls: list[str] = []

    def allowed(address: str, timeout: float = 2.0) -> None:
        calls.append(address)

    monkeypatch.setattr(probe, "_roundtrip", allowed)
    baseline = probe.run_probe("baseline", "192.0.2.10")
    assert baseline.loopback == "allowed"
    assert baseline.non_loopback == "allowed"
    assert calls == ["127.0.0.1", "192.0.2.10"]

    calls.clear()

    def blocked(address: str, timeout: float = 2.0) -> None:
        calls.append(address)
        if address != "127.0.0.1":
            raise probe.ConnectionAttemptError("synthetic firewall denial")

    monkeypatch.setattr(probe, "_roundtrip", blocked)
    enforced = probe.run_probe("enforced", "192.0.2.10")
    assert enforced.loopback == "allowed"
    assert enforced.non_loopback == "denied"
    assert enforced.non_loopback_error == "synthetic firewall denial"
    assert "not-tested" in enforced.dns
    assert calls == ["127.0.0.1", "192.0.2.10"]


def test_probe_cli_emits_machine_readable_local_only_evidence(monkeypatch, capsys):
    probe = _load_probe()
    monkeypatch.setattr(probe, "_roundtrip", lambda address, timeout=2.0: None)

    assert (
        probe.main(["--mode", "baseline", "--non-loopback-address", "192.0.2.10"])
        == 0
    )
    evidence = json.loads(capsys.readouterr().out)
    assert evidence == {
        "dns": (
            "not-tested: Windows DNS Client can mediate getaddrinfo, so an "
            "exact-program firewall rule is not reliable DNS-denial proof"
        ),
        "loopback": "allowed",
        "mode": "baseline",
        "non_loopback": "allowed",
        "non_loopback_error": None,
    }


def test_probe_sources_contain_no_external_network_target():
    sources = POWERSHELL_GATE.read_text(encoding="utf-8") + PROBE_PATH.read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "example.com",
        "example.invalid",
        "198.51.100.1",
        "203.0.113.1",
        "8.8.8.8",
        "1.1.1.1",
        "http://",
        "https://",
    ):
        assert forbidden not in sources
