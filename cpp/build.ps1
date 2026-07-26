<#
.SYNOPSIS
    Build the execution_engine C++ pybind11 module.

.DESCRIPTION
    Loads the MSVC x64 developer environment, configures with CMake + Ninja,
    compiles the .pyd module, and copies compile_commands.json to the cpp/
    directory for clangd IntelliSense.

    Run from the repo root or from cpp/ — $PSScriptRoot handles either.

.EXAMPLE
    # From repo root:
    .\cpp\build.ps1

    # From cpp/ directory:
    .\build.ps1
#>

$ErrorActionPreference = "Stop"

# ── Load MSVC x64 developer environment ──────────────────────────────────────
$vs = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" `
    -latest -property installationPath
if (-not $vs) {
    Write-Host "[ERROR] Visual Studio not found. Install the 'Desktop development with C++' workload." -ForegroundColor Red
    exit 1
}

Write-Host "[ENV] Loading MSVC x64 developer environment..." -ForegroundColor Cyan
Import-Module "$vs\Common7\Tools\Microsoft.VisualStudio.DevShell.dll"
Enter-VsDevShell -VsInstallPath $vs -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null

# Ninja is bundled with VS
$env:PATH = "$vs\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja;$env:PATH"

# ── Resolve active .venv paths ────────────────────────────────────────────────
$pythonExe  = python -c "import sys; print(sys.executable)"
$pythonRoot = Split-Path $pythonExe -Parent

$pybind11Dir = python -m pybind11 --cmakedir 2>$null
if ($LASTEXITCODE -ne 0 -or -not $pybind11Dir) {
    Write-Host "[ERROR] pybind11 not found in active Python environment." -ForegroundColor Red
    Write-Host "        Activate your .venv and run: pip install pybind11" -ForegroundColor Yellow
    exit 1
}

Write-Host "[INFO] Python  : $pythonExe" -ForegroundColor Cyan
Write-Host "[INFO] pybind11: $pybind11Dir" -ForegroundColor Cyan

# ── CMake configure ───────────────────────────────────────────────────────────
$cppDir = $PSScriptRoot
Set-Location $cppDir

Write-Host "[CMAKE] Configuring (Ninja, Release)..." -ForegroundColor Green
cmake -B build-ninja -G Ninja `
    -DCMAKE_BUILD_TYPE=Release `
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON `
    "-Dpybind11_DIR=$pybind11Dir" `
    "-DPython3_ROOT_DIR=$pythonRoot" `
    "-DPython3_EXECUTABLE=$pythonExe"

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] CMake configure failed." -ForegroundColor Red
    exit 1
}

# ── Build ─────────────────────────────────────────────────────────────────────
Write-Host "[CMAKE] Building..." -ForegroundColor Green
cmake --build build-ninja --config Release

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Build failed." -ForegroundColor Red
    exit 1
}

# ── Copy compile_commands.json for clangd ─────────────────────────────────────
if (Test-Path "build-ninja\compile_commands.json") {
    Copy-Item "build-ninja\compile_commands.json" ".\compile_commands.json" -Force
    Write-Host "[SUCCESS] compile_commands.json updated." -ForegroundColor Green
    Write-Host "[INFO] Restart clangd: Ctrl+Shift+P -> 'clangd: Restart language server'" -ForegroundColor Cyan
}

Write-Host "[SUCCESS] Build complete. .pyd lives in cpp/build-ninja/" -ForegroundColor Green
Write-Host "[NEXT] Run: python test_phase0.py" -ForegroundColor Yellow
