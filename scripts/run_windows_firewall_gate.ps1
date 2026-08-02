#Requires -Version 7.0

<#
.SYNOPSIS
Runs an opt-in, exact-executable Windows Firewall isolation probe.

.DESCRIPTION
This script requires an elevated PowerShell 7 session because it temporarily
creates one uniquely named outbound Block rule for one resolved Python
executable. It validates loopback and a connection back to this workstation's
own non-loopback IPv4 address. The rule is removed and its absence verified in
a finally block.

The script deliberately does not claim DNS-denial evidence. Windows DNS Client
can perform name resolution on behalf of a process, so a rule scoped only to
python.exe cannot reliably prove that getaddrinfo traffic was denied. The JSON
result therefore reports the complete addendum network gate as incomplete and
the script exits with code 2 after successful cleanup. Validation, socket-probe,
or cleanup failures terminate with a different nonzero failure.

Do not run this script without explicit approval to make the temporary
firewall-rule change.

.PARAMETER PythonPath
The Python executable to constrain. If omitted, the audited workspace .venv
interpreter is selected. A Python executable outside this workspace must be
supplied explicitly.

.PARAMETER LocalAddress
An optional non-loopback IPv4 address assigned to this workstation. If omitted,
the script selects an address on an up network adapter.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string] $PythonPath,

    [Parameter()]
    [string] $LocalAddress
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$workspaceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonWasExplicit = $PSBoundParameters.ContainsKey('PythonPath')
$probePath = Join-Path $PSScriptRoot '_windows_firewall_probe.py'
$ruleName = 'LocalDocForge-ReleaseGate-{0}-{1}' -f $PID, ([guid]::NewGuid().ToString('N'))
$ruleAttempted = $false
$primaryFailure = $null
$cleanupFailure = $null
$baseline = $null
$enforced = $null

function Test-PathWithinWorkspace {
    param(
        [Parameter(Mandatory)]
        [string] $Candidate,
        [Parameter(Mandatory)]
        [string] $Workspace
    )

    $relative = [System.IO.Path]::GetRelativePath($Workspace, $Candidate)
    return -not (
        [System.IO.Path]::IsPathRooted($relative) -or
        $relative -eq '..' -or
        $relative.StartsWith("..$([System.IO.Path]::DirectorySeparatorChar)")
    )
}

function Invoke-LocalProbe {
    param(
        [Parameter(Mandatory)]
        [ValidateSet('baseline', 'enforced')]
        [string] $Mode,
        [Parameter(Mandatory)]
        [string] $Address,
        [Parameter(Mandatory)]
        [string] $Executable
    )

    $output = & $Executable -I $probePath --mode $Mode --non-loopback-address $Address
    if ($LASTEXITCODE -ne 0) {
        throw "Local firewall probe '$Mode' failed with exit code $LASTEXITCODE."
    }
    return (($output -join [Environment]::NewLine) | ConvertFrom-Json)
}

if (-not $IsWindows) {
    throw 'The Windows Firewall release gate can run only on Windows.'
}
if ([Environment]::OSVersion.Version.Build -lt 22000) {
    throw 'The primary release gate requires Windows 11 (build 22000 or newer).'
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw 'The primary release gate requires a 64-bit Windows installation.'
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this opt-in gate from an explicitly approved elevated PowerShell 7 session.'
}

foreach ($commandName in @(
    'Get-NetFirewallApplicationFilter',
    'Get-NetFirewallProfile',
    'Get-NetFirewallRule',
    'New-NetFirewallRule',
    'Remove-NetFirewallRule'
)) {
    if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) {
        throw "Required Windows Firewall cmdlet is unavailable: $commandName"
    }
}

$firewallService = Get-Service -Name 'MpsSvc' -ErrorAction Stop
if ($firewallService.Status -ne 'Running') {
    throw 'Windows Defender Firewall service (MpsSvc) is not running.'
}
$firewallProfiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
$requiredProfiles = @('Domain', 'Private', 'Public')
foreach ($profileName in $requiredProfiles) {
    $profile = $firewallProfiles | Where-Object Name -EQ $profileName
    if (-not $profile -or -not $profile.Enabled) {
        throw "Windows Firewall profile '$profileName' is unavailable or disabled."
    }
}

if (-not $pythonWasExplicit) {
    $PythonPath = Join-Path $workspaceRoot '.venv\Scripts\python.exe'
}
$candidatePython = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
if ([System.IO.Path]::GetExtension($candidatePython) -ine '.exe') {
    throw 'PythonPath must resolve to an executable (.exe) file.'
}
$reportedPython = & $candidatePython -I -c 'import os, sys; print(os.path.realpath(sys.executable))'
if ($LASTEXITCODE -ne 0 -or -not $reportedPython) {
    throw 'The selected Python executable did not report its executable path.'
}
$resolvedPython = (Resolve-Path -LiteralPath ($reportedPython[-1].Trim()) -ErrorAction Stop).Path
if (-not $pythonWasExplicit -and -not (Test-PathWithinWorkspace $resolvedPython $workspaceRoot)) {
    throw 'The default Python executable resolved outside the workspace; supply it explicitly.'
}
$pythonScope = if (Test-PathWithinWorkspace $resolvedPython $workspaceRoot) {
    'workspace-relative executable'
} else {
    'explicit external executable; absolute path omitted'
}
$pythonHash = (Get-FileHash -LiteralPath $resolvedPython -Algorithm SHA256).Hash.ToLowerInvariant()

if ($LocalAddress) {
    $addressRecord = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $LocalAddress -ErrorAction Stop
    if ($addressRecord.IPAddress -ne $LocalAddress) {
        throw 'LocalAddress must be an IPv4 address assigned to this workstation.'
    }
} else {
    $upIndexes = @(
        Get-NetAdapter -ErrorAction Stop |
            Where-Object Status -EQ 'Up' |
            Select-Object -ExpandProperty ifIndex
    )
    $addressRecord = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object {
            $_.InterfaceIndex -in $upIndexes -and
            -not $_.SkipAsSource -and
            $_.IPAddress -ne '127.0.0.1' -and
            $_.IPAddress -notlike '0.*' -and
            $_.IPAddress -notlike '224.*'
        } |
        Sort-Object InterfaceIndex, IPAddress |
        Select-Object -First 1
    if (-not $addressRecord) {
        throw 'No usable non-loopback IPv4 address exists on an up local adapter.'
    }
    $LocalAddress = $addressRecord.IPAddress
}

try {
    $baseline = Invoke-LocalProbe -Mode baseline -Address $LocalAddress -Executable $resolvedPython

    $ruleAttempted = $true
    New-NetFirewallRule `
        -Name $ruleName `
        -DisplayName $ruleName `
        -Group 'LocalDocForge Release Gate' `
        -Direction Outbound `
        -Action Block `
        -Enabled True `
        -Profile Any `
        -Program $resolvedPython `
        -Protocol Any `
        -PolicyStore PersistentStore | Out-Null

    $storedRule = Get-NetFirewallRule `
        -Name $ruleName `
        -PolicyStore PersistentStore `
        -ErrorAction Stop
    $rule = Get-NetFirewallRule -Name $ruleName -PolicyStore ActiveStore -ErrorAction Stop
    $applicationFilter = $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
    if (
        $storedRule.Direction -ne 'Outbound' -or
        $storedRule.Action -ne 'Block' -or
        $rule.Direction -ne 'Outbound' -or
        $rule.Action -ne 'Block' -or
        $rule.Enabled -ne 'True' -or
        [System.IO.Path]::GetFullPath($applicationFilter.Program) -ine $resolvedPython
    ) {
        throw 'The effective firewall rule does not match the requested exact-program block.'
    }

    $enforced = Invoke-LocalProbe -Mode enforced -Address $LocalAddress -Executable $resolvedPython
} catch {
    $primaryFailure = $_
} finally {
    if ($ruleAttempted) {
        try {
            Get-NetFirewallRule -Name $ruleName -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
                Out-Null
            Get-NetFirewallRule `
                -Name $ruleName `
                -PolicyStore PersistentStore `
                -ErrorAction SilentlyContinue |
                Remove-NetFirewallRule -Confirm:$false -ErrorAction Stop
            $remainingStoredRule = Get-NetFirewallRule `
                -Name $ruleName `
                -PolicyStore PersistentStore `
                -ErrorAction SilentlyContinue
            $remainingActiveRule = Get-NetFirewallRule `
                -Name $ruleName `
                -PolicyStore ActiveStore `
                -ErrorAction SilentlyContinue
            if ($remainingStoredRule -or $remainingActiveRule) {
                throw "Temporary firewall rule still exists after cleanup: $ruleName"
            }
        } catch {
            $cleanupFailure = $_
        }
    }
}

if ($cleanupFailure) {
    throw "Firewall cleanup failed; inspect and remove '$ruleName': $($cleanupFailure.Exception.Message)"
}
if ($primaryFailure) {
    throw $primaryFailure
}

[ordered]@{
    schema = 1
    timestamp_utc = [DateTime]::UtcNow.ToString('o')
    os = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
    architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    powershell = $PSVersionTable.PSVersion.ToString()
    python_executable_name = [System.IO.Path]::GetFileName($resolvedPython)
    python_executable_sha256 = $pythonHash
    python_scope = $pythonScope
    firewall_mechanism = 'Windows Defender Firewall PersistentStore rule verified in ActiveStore'
    firewall_rule_name = $ruleName
    firewall_rule_removed = $true
    local_non_loopback_address = 'host-assigned IPv4; value omitted'
    baseline = $baseline
    enforced = $enforced
    dns = [ordered]@{
        status = 'not_verified'
        reason = 'Windows DNS Client may mediate getaddrinfo outside an exact python.exe rule.'
    }
    windows_addendum_network_gate = 'incomplete_dns_proof'
    process_exit_code = 2
} | ConvertTo-Json -Depth 5
exit 2
