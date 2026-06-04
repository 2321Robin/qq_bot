from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
import tomllib

_DISTRIBUTION_NAME = "qq-bot"
_UNKNOWN_VERSION = "0.0.0+unknown"
_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


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


__version__ = get_version()
