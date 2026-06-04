import importlib.metadata
import tomllib
from pathlib import Path

import pytest

from qq_bot import __version__
from qq_bot import version as version_module
from qq_bot.version import get_version


def test_package_version_matches_project_version() -> None:
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as pyproject:
        expected = tomllib.load(pyproject)["project"]["version"]

    assert get_version() == expected
    assert __version__ == expected


def test_get_version_falls_back_to_installed_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    missing_path = type("MissingPath", (), {"is_file": lambda self: False})()

    monkeypatch.setattr(version_module, "_PYPROJECT_PATH", missing_path)
    monkeypatch.setattr(version_module, "distribution_version", lambda name: "1.2.3")
    version_module.get_version.cache_clear()

    try:
        assert get_version() == "1.2.3"
    finally:
        version_module.get_version.cache_clear()


def test_get_version_has_unknown_fallback_when_project_and_distribution_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_distribution(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    missing_path = type("MissingPath", (), {"is_file": lambda self: False})()

    monkeypatch.setattr(version_module, "_PYPROJECT_PATH", missing_path)
    monkeypatch.setattr(version_module, "distribution_version", missing_distribution)
    version_module.get_version.cache_clear()

    try:
        assert get_version() == "0.0.0+unknown"
    finally:
        version_module.get_version.cache_clear()
