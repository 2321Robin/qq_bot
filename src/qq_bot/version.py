import subprocess
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
import tomllib

_DISTRIBUTION_NAME = "qq-bot"
_UNKNOWN_VERSION = "0.0.0+unknown"
_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_pyproject_version() -> str | None:
    if not _PYPROJECT_PATH.is_file():
        return None

    with _PYPROJECT_PATH.open("rb") as pyproject:
        project = tomllib.load(pyproject).get("project", {})

    value = project.get("version")
    if isinstance(value, str) and value:
        return value
    return None


@lru_cache(maxsize=1)
def get_version() -> str:
    pyproject_version = _read_pyproject_version()
    if pyproject_version is not None:
        return pyproject_version

    try:
        return distribution_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _UNKNOWN_VERSION


def _read_git_commit() -> str | None:
    """部署工作区是 git checkout（systemd /opt/qq_bot），读当前提交短哈希；
    容器镜像等无 .git 的环境返回 None，任何失败都降级为 None 绝不抛错。

    版本号只在发版时更新，而部署随时在滚——/version 只报 0.x.y 会看不出
    线上到底跑的哪个提交（2026-10-26 部署者裁决补上）。"""
    if not (_REPO_ROOT / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = completed.stdout.strip()
    return commit or None


@lru_cache(maxsize=1)
def get_commit() -> str | None:
    return _read_git_commit()


__version__ = get_version()
