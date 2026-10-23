import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest


def checker():
    path = Path(__file__).resolve().parents[1] / "scripts/check_publication.py"
    spec = importlib.util.spec_from_file_location("publication_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "member",
    [
        "logs/run/output.txt",
        "workspace/bloom.sqlite3",
        "config/local.toml",
        "../outside.txt",
        "/absolute.txt",
        "unexpected-untracked-file.txt",
    ],
)
def test_sdist_rejects_generated_private_or_untracked_files(tmp_path, member):
    path = tmp_path / "bloom-0.1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        entry = tarfile.TarInfo("bloom-0.1.0/" + member if not member.startswith("/") else member)
        data = b"synthetic private fixture"
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    assert checker().check_package(path, {"README.md", "src/bloom/__init__.py"})


def test_sdist_accepts_reviewed_source_and_generated_package_metadata(tmp_path):
    path = tmp_path / "bloom-0.1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name in ("README.md", "src/bloom/__init__.py", "PKG-INFO"):
            entry = tarfile.TarInfo("bloom-0.1.0/" + name)
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b"x"))
    assert not checker().check_package(path, {"README.md", "src/bloom/__init__.py"})


def test_wheel_rejects_workspace_even_beneath_package_directory(tmp_path):
    path = tmp_path / "bloom-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("bloom/workspace/bloom.sqlite3", b"not a real database")
    assert checker().check_package(path, set())
