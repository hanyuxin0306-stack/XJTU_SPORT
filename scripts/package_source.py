"""Package an explicit set of public project files; never include local state."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import unquote, urlsplit
import zipfile


ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILES = (
    ".gitignore",
    ".gitattributes",
    "README.md",
    "使用说明.txt",
    "config.example.json",
    "sport.py",
    "sport.spec",
    "build.ps1",
    "requirements.txt",
    "requirements-build.txt",
    "logo.ico",
    "docs/assets/main-window.png",
    "scripts/package_source.py",
    "tests/test_sport.py",
    "tests/test_sport_import.py",
    "tests/test_sport_login.py",
    "tests/test_sport_login_dom.py",
)
WINDOWS_FILES = (
    "sport.exe", "README.md", "使用说明.txt", "config.example.json", "logo.ico",
    "docs/assets/main-window.png",
)
PRIVATE_PARTS = {
    "config.json", ".env", ".venv", "venv", "env", ".git", "__pycache__",
    "backups", "build", "dist", "output", "login_diagnostics", "diagnostics",
    "logs", "cookies", "sessions", "browser_profile", "playwright_edge_profile",
}


def public_path(name: str, *, allow_executable: bool = False) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError(f"Invalid public path: {name}")
    if any(part.lower() in PRIVATE_PARTS for part in relative.parts):
        raise ValueError(f"Private runtime path cannot be packaged: {name}")
    suffix = relative.suffix.lower()
    if suffix in {".log", ".pyc", ".pyo", ".docx", ".har", ".sqlite", ".db"}:
        raise ValueError(f"Runtime or private file cannot be packaged: {name}")
    if suffix == ".exe" and not (allow_executable and name == "sport.exe"):
        raise ValueError(f"Unexpected executable: {name}")
    path = ROOT.joinpath(*relative.parts)
    resolved = path.resolve()
    if ROOT not in resolved.parents or not path.is_file():
        raise ValueError(f"Missing or external public file: {name}")
    current = path
    while current != ROOT:
        if current.is_symlink():
            raise ValueError(f"Symbolic links are not packaged: {name}")
        current = current.parent
    return path


def check_example() -> None:
    example = json.loads(public_path("config.example.json").read_text(encoding="utf-8"))
    if example.get("user_id", "") or example.get("password", ""):
        raise ValueError("The example configuration contains credentials")
    if example.get("remember") is not False:
        raise ValueError("The example must disable credential persistence")
    for key in ("token", "access_token", "refresh_token", "cookie", "cookies", "session"):
        if example.get(key):
            raise ValueError("The example configuration contains authentication state")


def local_readme_links() -> set[str]:
    text = public_path("README.md").read_text(encoding="utf-8")
    # Ignore code examples, then inspect inline links/images and reference URLs.
    text = re.sub(r"```[^\n]*\n[\s\S]*?```", "", text)
    targets = re.findall(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)", text)
    targets.extend(re.findall(r"^\s*\[[^\]\n]+\]:\s*(\S+)", text, re.MULTILINE))
    paths = set()
    for target in targets:
        target = target.strip("<>")
        url = urlsplit(target)
        if url.scheme or url.netloc or not url.path:
            continue
        name = unquote(url.path)
        if name.startswith("./"):
            name = name[2:]
        if name not in SOURCE_FILES:
            raise ValueError(f"README link is missing from the public allowlist: {name}")
        public_path(name)
        paths.add(name)
    return paths


def check_files() -> dict:
    for name in SOURCE_FILES:
        public_path(name)
    check_example()
    links = local_readme_links()
    return {
        "source_file_count": len(SOURCE_FILES),
        "readme_local_links_checked": sorted(links),
        "example_credentials_empty": True,
        "local_configuration_packaged": False,
    }


def write_archive(filename: str, names: tuple[str, ...], *, executable: bool = False) -> dict:
    output = ROOT / "release"
    if output.is_symlink():
        raise ValueError("Release directory cannot be a symbolic link")
    output.mkdir(exist_ok=True)
    archive = output / filename
    if archive.is_symlink():
        raise ValueError("Archive target cannot be a symbolic link")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for name in names:
            target.write(public_path(name, allow_executable=executable), "XJTU_SPORT/" + name)
    with zipfile.ZipFile(archive) as target:
        expected = ["XJTU_SPORT/" + name for name in names]
        if target.namelist() != expected:
            raise RuntimeError("Archive manifest differs from the allowlist")
        if target.testzip() is not None:
            raise RuntimeError("Archive validation failed")
    return {
        "path": str(archive.relative_to(ROOT)),
        "files": len(names),
        "bytes": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate public files without writing archives")
    parser.add_argument("--with-windows", action="store_true", help="Also package the existing sport.exe")
    args = parser.parse_args()
    report = check_files()
    if args.with_windows:
        for name in WINDOWS_FILES:
            public_path(name, allow_executable=True)
    if not args.check:
        report["archives"] = [write_archive("XJTU_SPORT_source.zip", SOURCE_FILES)]
        if args.with_windows:
            report["archives"].append(write_archive("XJTU_SPORT_windows.zip", WINDOWS_FILES, executable=True))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
