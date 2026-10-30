# -*- mode: python ; coding: utf-8 -*-
"""Build the classic public release without embedding config.json."""
from pathlib import Path
import os
import sys
from PyInstaller.utils.hooks import collect_all

project_dir = Path(SPECPATH)
selenium_datas, selenium_binaries, selenium_hiddenimports = collect_all("selenium")

# A venv based on Conda needs its base environment's native runtime DLLs.
conda_bin_dir = Path(sys.base_prefix) / "Library" / "bin"
runtime_binaries = []
if conda_bin_dir.is_dir():
    os.environ["PATH"] = str(conda_bin_dir) + os.pathsep + os.environ.get("PATH", "")
    for dll_name in ("libmpdec-4.dll", "ffi.dll", "tk86t.dll", "tcl86t.dll"):
        dll_path = conda_bin_dir / dll_name
        if dll_path.is_file():
            runtime_binaries.append((str(dll_path), "."))

a = Analysis(
    [str(project_dir / "sport.py")],
    pathex=[str(project_dir)],
    binaries=selenium_binaries + runtime_binaries,
    datas=[(str(project_dir / "logo.ico"), ".")] + selenium_datas,
    hiddenimports=selenium_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="sport",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(project_dir / "logo.ico")],
)
