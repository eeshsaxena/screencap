# Install screencap CLI binary for Windows.
# Usage: irm https://storage.googleapis.com/screencap-releases/releases/install.ps1 | iex
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Validate USERPROFILE
if (-not $env:USERPROFILE) {
    Write-Error "USERPROFILE is not set. Aborting."
    exit 1
}

$InstallDir = Join-Path $env:USERPROFILE ".screencap\bin"
$Arch = "windows-x86_64"

# Distribution base URL
$DistBaseUrl = if ($env:SCREENCAP_DIST_URL) { $env:SCREENCAP_DIST_URL } else {
    "https://storage.googleapis.com/screencap-releases/releases"
}

# Version: env var override or auto-detect from latest.txt
$Version = $env:SCREENCAP_VERSION
if (-not $Version) {
    $Version = (Invoke-WebRequest -Uri "$DistBaseUrl/latest.txt" -UseBasicParsing).Content.Trim()
    if (-not $Version) {
        Write-Error "Could not detect latest version."
        exit 1
    }
}

$BaseUrl = "$DistBaseUrl/v$Version"
$ZipName = "screencap-$Version-$Arch.zip"

Write-Host "Installing screencap $Version for $Arch..."

# Download to temp directory
$TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) "screencap-install-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null

try {
    $ZipPath = Join-Path $TmpDir $ZipName
    $ChecksumPath = Join-Path $TmpDir "checksums.sha256"

    Invoke-WebRequest -Uri "$BaseUrl/$ZipName" -OutFile $ZipPath -UseBasicParsing
    Invoke-WebRequest -Uri "$BaseUrl/checksums.sha256" -OutFile $ChecksumPath -UseBasicParsing

    # Verify checksum
    $ExpectedLine = Get-Content $ChecksumPath | Where-Object { $_ -match [regex]::Escape($ZipName) }
    if (-not $ExpectedLine) {
        Write-Error "No checksum found for $ZipName in checksums file."
        exit 1
    }
    $ExpectedHash = ($ExpectedLine -split '\s+')[0]
    $ActualHash = (Get-FileHash $ZipPath -Algorithm SHA256).Hash.ToLower()

    if ($ActualHash -ne $ExpectedHash) {
        Write-Error "Checksum verification failed.`nExpected: $ExpectedHash`nGot:      $ActualHash"
        exit 1
    }
    Write-Host "Checksum verified."

    # Extract
    if (Test-Path $InstallDir) {
        Remove-Item -Recurse -Force (Join-Path $InstallDir "screencap") -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Expand-Archive -Path $ZipPath -DestinationPath $InstallDir -Force

    # Add to user PATH (idempotent)
    $BinPath = Join-Path $InstallDir "screencap"
    if ($env:SCREENCAP_NO_MODIFY_PATH -ne "1") {
        $CurrentPath = [Environment]::GetEnvironmentVariable("PATH", "User")
        if ($CurrentPath -notlike "*$BinPath*") {
            [Environment]::SetEnvironmentVariable("PATH", "$BinPath;$CurrentPath", "User")
            Write-Host "Added $BinPath to user PATH."
            Write-Host "Restart your terminal for PATH changes to take effect."
        }
    }

    # Verify installation
    $ExePath = Join-Path $BinPath "screencap.exe"
    if (Test-Path $ExePath) {
        $InstalledVersion = & $ExePath --version 2>&1
        Write-Host ""
        Write-Host "screencap installed successfully! ($InstalledVersion)"
    } else {
        Write-Error "Installation verification failed: $ExePath not found."
        exit 1
    }

    Write-Host "Run 'screencap --help' to get started."
    Write-Host ""
} finally {
    Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue
}
