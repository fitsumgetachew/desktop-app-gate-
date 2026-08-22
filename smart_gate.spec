# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

hiddenimports = []
hiddenimports += collect_submodules("cv2")
# face_recognition is imported *inside* the functions that need it so a
# station without dlib still runs the gate — which also means PyInstaller's
# static analysis never sees it. Name it explicitly or the packaged build
# silently recognises nobody.
hiddenimports += ["face_recognition", "face_recognition_models", "pyttsx3"]
hiddenimports += collect_submodules("pyttsx3.drivers")


a = Analysis(
    ["smart_gate/main.py"],
    pathex=[],
    binaries=[],
    # Runtime assets loaded by path, not importable — PyInstaller cannot infer
    # them. The alarm siren is required for the BLACKLISTED state to be audible
    # in a packaged build.
    datas=[
        # dlib's shape predictor / recognition .dat files, resolved at runtime
        # through pkg_resources — invisible to PyInstaller without this.
        *collect_data_files("face_recognition_models"),
        ("smart_gate/assets/sounds", "smart_gate/assets/sounds"),
        ("smart_gate/assets/models", "smart_gate/assets/models"),
        ("smart_gate/assets/logo_dark.png", "smart_gate/assets"),
        ("smart_gate/assets/logo_light.svg", "smart_gate/assets"),
        # Stylesheet arrows, referenced by absolute path from theme.py.
        ("smart_gate/assets/chevron_down.svg", "smart_gate/assets"),
        ("smart_gate/assets/chevron_up.svg", "smart_gate/assets"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="smart-gate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="smart-gate",
)
