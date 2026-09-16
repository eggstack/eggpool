<#
.SYNOPSIS
  Download, verify, and execute the pinned `eggpool-connect` helper on Windows.

.DESCRIPTION
  This script only selects, downloads, verifies, and executes the native
  `eggpool-connect` helper for one pinned EggPool release. It performs no
  client config mutation itself; the verified helper owns plan, backup,
  install, verification, and rollback.

  Safety properties (covered by tests/tooling/test_connect_release.py):
  - HTTPS only over the default .NET TLS stack; no validation bypass and no
    evaluation of profile text or network bytes as script code.
  - The helper SHA-256 is verified with Get-FileHash against the release
    SHA256SUMS before anything is executed; a missing entry or mismatch
    fails closed.
  - The profile token is passed to the helper as a data argument only and is
    never evaluated, never printed, and never written to a file by this
    script. No Python is required.

.EXAMPLE
  & eggpool-connect.ps1 -Version '0.8.0' -Profile 'epc1...'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Profile,

    [Parameter(Mandatory = $true)]
    [string]$Version,

    [string]$Repo = 'eggstack/eggpool',

    [ValidateSet('', 'codex', 'opencode')]
    [string]$Client = ''
)

$ErrorActionPreference = 'Stop'

if ($Profile -notlike 'epc1.*') {
    throw "eggpool-connect bootstrap: profile does not look like an epc1 connection token"
}
if ($Version -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$' -or $Version -like '*..*') {
    throw 'eggpool-connect bootstrap: version contains unsupported characters'
}
if ($Repo -notmatch '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$') {
    throw 'eggpool-connect bootstrap: repo contains unsupported characters'
}
if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [System.Runtime.InteropServices.OSPlatform]::Windows)) {
    throw "eggpool-connect bootstrap: this script is for Windows (macOS/Linux use eggpool-connect.sh)"
}
$arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture
if ($arch -ne [System.Runtime.InteropServices.Architecture]::X64) {
    throw "eggpool-connect bootstrap: unsupported Windows architecture '$arch' (x64 only)"
}

$asset = "eggpool-connect-$Version-windows-x86_64.exe"
$base = "https://github.com/$Repo/releases/download/v$Version"
$tempRoot = if ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
$workDir = New-Item -ItemType Directory -Path (
    Join-Path $tempRoot ("eggpool-connect-" + [System.Guid]::NewGuid().ToString('N'))
)
try {
    $helperPath = Join-Path $workDir.FullName 'helper.exe'
    $sumsPath = Join-Path $workDir.FullName 'SHA256SUMS'
    Invoke-WebRequest -Uri "$base/$asset" -OutFile $helperPath -UseBasicParsing
    Invoke-WebRequest -Uri "$base/SHA256SUMS" -OutFile $sumsPath -UseBasicParsing

    $expected = $null
    foreach ($line in Get-Content -Path $sumsPath) {
        $parts = $line -split '\s+'
        if ($parts.Count -ge 2 -and $parts[1].EndsWith($asset)) {
            $expected = $parts[0].ToLowerInvariant()
            break
        }
    }
    if (-not $expected -or $expected -notmatch '^[0-9a-f]{64}$') {
        throw "eggpool-connect bootstrap: checksum entry is missing or malformed for $asset"
    }
    $actual = (Get-FileHash -Path $helperPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expected -ne $actual) {
        throw "eggpool-connect bootstrap: SHA-256 mismatch for $asset (refusing to execute)"
    }

    $helperArgs = @('install', '--profile', $Profile)
    if ($Client -ne '') {
        $helperArgs += @('--client', $Client)
    }
    & $helperPath @helperArgs
    $status = $LASTEXITCODE
}
finally {
    Remove-Item -Recurse -Force -Path $workDir.FullName -ErrorAction SilentlyContinue
}
exit $status
