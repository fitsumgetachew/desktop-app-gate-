# Smart Gate — one-shot environment setup for Windows 10/11 (x64).
#
# Run from the repository root in PowerShell:
#     powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
#
# What it does, and why the order matters:
#   1. Creates .venv with the "py" launcher (Python 3.12 x64 required).
#   2. Installs dlib-bin — PREBUILT dlib for Windows. This is the whole trick:
#      plain "pip install dlib" compiles from source and needs Visual Studio
#      C++ Build Tools + CMake (~30 min of setup); dlib-bin needs nothing.
#   3. Installs face-recognition with --no-deps so pip does not see its
#      "dlib" requirement and try to compile it anyway (dlib-bin satisfies the
#      import, but pip does not know that — different distribution name).
#   4. Installs everything else from requirements-windows.txt.
#   5. Installs the Windows speech deps (pyttsx3 drives the built-in SAPI5
#      voices through comtypes/pywin32 — no espeak on Windows).

$ErrorActionPreference = "Stop"

Write-Host "== Smart Gate Windows setup ==" -ForegroundColor Cyan

# ── 1. Python check + venv ────────────────────────────────────────────
$pyVersion = & py -3.12 --version 2>$null
if (-not $pyVersion) {
    Write-Host "Python 3.12 (64-bit) is required and was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ and tick"
    Write-Host "'Add python.exe to PATH' in the installer, then re-run this script."
    exit 1
}
Write-Host "Found $pyVersion"

if (-not (Test-Path ".venv")) {
    py -3.12 -m venv .venv
    Write-Host "Created .venv"
}
$pip = ".\.venv\Scripts\python.exe -m pip"

# ── 2-5. Installs, in dependency-safe order ──────────────────────────
Invoke-Expression "$pip install --upgrade pip wheel"
Invoke-Expression "$pip install `"setuptools<81`""

Write-Host "Installing prebuilt dlib (no compiler needed)..." -ForegroundColor Cyan
Invoke-Expression "$pip install dlib-bin"

Write-Host "Installing face recognition (without its source-dlib pin)..." -ForegroundColor Cyan
Invoke-Expression "$pip install --no-deps face-recognition==1.3.0"
Invoke-Expression "$pip install face_recognition_models==0.3.0 Click Pillow"

Write-Host "Installing the rest..." -ForegroundColor Cyan
Invoke-Expression "$pip install -r requirements-windows.txt"

Write-Host "Installing Windows speech backend (SAPI5)..." -ForegroundColor Cyan
Invoke-Expression "$pip install pywin32 comtypes"

# ── Config file ──────────────────────────────────────────────────────
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example — edit it before running."
}

# ── Smoke test ───────────────────────────────────────────────────────
Write-Host "Verifying the stack imports..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -c @"
import cv2, numpy, onnxruntime, PySide6, requests, pyttsx3
import face_recognition
print('cv2', cv2.__version__)
print('face_recognition OK (dlib prebuilt)')
print('ALL IMPORTS OK')
"@

Write-Host ""
Write-Host "Setup complete. Run the app with:" -ForegroundColor Green
Write-Host "    .\.venv\Scripts\python.exe -m smart_gate"
