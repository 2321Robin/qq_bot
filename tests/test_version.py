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


def test_get_commit_returns_short_hash_from_git_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(version_module, "_read_git_commit", lambda: "8f39936")
    version_module.get_commit.cache_clear()

    try:
        assert version_module.get_commit() == "8f39936"
    finally:
        version_module.get_commit.cache_clear()


def test_get_commit_degrades_to_none_without_git_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """容器镜像等无 .git 的环境：get_commit 必须安静地返回 None。"""
    monkeypatch.setattr(version_module, "_REPO_ROOT", tmp_path)
    version_module.get_commit.cache_clear()

    try:
        assert version_module.get_commit() is None
    finally:
        version_module.get_commit.cache_clear()


def test_get_commit_survives_git_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """git 子进程失败（超时/找不到可执行文件）降级 None，绝不抛错。"""

    def boom(*args, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr(version_module.subprocess, "run", boom)
    version_module.get_commit.cache_clear()

    try:
        assert version_module.get_commit() is None
    finally:
        version_module.get_commit.cache_clear()
