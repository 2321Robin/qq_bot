"""Public release regression tests.

These tests check that controlled source files do not contain
deployment-private values and that required public-release files exist.

This is NOT a substitute for Gitleaks — Gitleaks is the authoritative
secret scanner for the full repository and its history.
"""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

CONTROLLED_FILES = [
    "start_all.ps1",
    "stop_all.ps1",
    "startup.example.ps1",
    "README.md",
    "PRIVACY.md",
    "LICENSE",
    "DATA_LICENSE.md",
    "docs/public-release/release-checklist.md",
    "docs/public-release/screenshot-redaction-checklist.md",
]

# Files that must exist for public release
REQUIRED_FILES = [
    "startup.example.ps1",
    ".gitleaks.toml",
    "LICENSE",
    "DATA_LICENSE.md",
    "PRIVACY.md",
    "docs/public-release/release-checklist.md",
    "docs/public-release/screenshot-redaction-checklist.md",
]


def _check_controlled_files(pattern: str, violation: str) -> list[str]:
    """Check all controlled files for a pattern. Return matching files."""
    hits = []
    for path_str in CONTROLLED_FILES:
        path = ROOT / path_str
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if pattern in text:
            hits.append(f"{path_str}: contains {violation}")
    return hits


def test_no_personal_user_directory() -> None:
    """Controlled files must not contain C:\\Users\\<name> paths."""
    hits = _check_controlled_files("C:\\Users\\", "a personal Windows user directory (C:\\Users\\)")
    assert not hits, "\n".join(hits)


def test_no_qq_account_literal() -> None:
    """Controlled files must not contain QQ account numbers.

    Checks both quoted literals and NapCatAccount assignment syntax.
    """
    import re

    hits = []
    for path_str in CONTROLLED_FILES:
        path = ROOT / path_str
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Check NapCatAccount assignment with a literal number value
            if re.search(r'NapCatAccount\s*=\s*\d', stripped):
                hits.append(f"{path_str}: NapCatAccount assignment with literal number")
            # Check standalone quoted numbers (6+ digits)
            matches = re.findall(r'["\']\d\d\d\d\d\d+["\']', stripped)
            for m in matches:
                # 8081 is the default bot port — safe
                if m in ('"8081"', "'8081'"):
                    continue
                hits.append(f"{path_str}: potential QQ account literal '{m}'")
    assert not hits, "\n".join(hits)


def test_no_webui_token_url() -> None:
    """Controlled files must not contain NapCat WebUI token URLs."""
    hits = _check_controlled_files(
        "webui?token=", "a NapCat WebUI token URL (webui?token=)"
    )
    assert not hits, "\n".join(hits)


def test_no_startup_local_tracked() -> None:
    """startup.local.ps1 must not be tracked by Git."""
    result = subprocess.run(
        ["git", "ls-files", "startup.local.ps1"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, "git ls-files failed"
    assert result.stdout.strip() == "", "startup.local.ps1 is tracked by Git"


def test_no_env_tracked() -> None:
    """.env must not be tracked by Git."""
    result = subprocess.run(
        ["git", "ls-files", ".env"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, "git ls-files failed"
    assert result.stdout.strip() == "", ".env is tracked by Git"


def test_no_chat_database_tracked() -> None:
    """Chat SQLite database must not be tracked by Git."""
    result = subprocess.run(
        ["git", "ls-files", "data/chat_memory.sqlite3"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, "git ls-files failed"
    assert result.stdout.strip() == "", "chat_memory.sqlite3 is tracked by Git"


def test_required_files_exist() -> None:
    """All required public-release files must exist."""
    missing = []
    for path_str in REQUIRED_FILES:
        if not (ROOT / path_str).exists():
            missing.append(path_str)
    assert not missing, "Missing required files:\n" + "\n".join(missing)


def test_gitleaks_toml_no_tokens() -> None:
    """.gitleaks.toml must not contain any real tokens or account numbers."""
    path = ROOT / ".gitleaks.toml"
    if not path.exists():
        return  # will be caught by test_required_files_exist
    import re
    text = path.read_text(encoding="utf-8")
    # Must not contain a NapCat token (hex token preceded by webui)
    assert not re.search(r"webui\\?token=['\"]?[0-9a-f]{12,}", text), \
        ".gitleaks.toml contains a NapCat WebUI token"
    # Must not contain a QQ account number literal
    assert not re.search(r"\d{9,}", text), \
        ".gitleaks.toml contains a QQ account number"


def test_startup_example_no_real_values() -> None:
    """startup.example.ps1 must not contain any real paths or account numbers."""
    path = ROOT / "startup.example.ps1"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    assert "C:\\Users\\" not in text, "startup.example.ps1 contains a personal path"
    assert "NapCatAccount" in text
    assert "NapCatDir" in text
    assert "BotPort" in text


def test_license_file_has_valid_content() -> None:
    """LICENSE must contain MIT license text."""
    path = ROOT / "LICENSE"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Copyright (c) 2026" in text
    assert "Robin" in text


def test_data_license_file_has_required_sections() -> None:
    """DATA_LICENSE.md must cover all content categories."""
    path = ROOT / "DATA_LICENSE.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    assert "BWiki" in text, "DATA_LICENSE.md missing BWiki section"
    assert "game assets" in text.lower() or "Game assets" in text or "game sprites" in text.lower()
    assert "Pet" in text or "pet cards" in text.lower()
    assert "MIT" in text or "MIT License" in text
    assert "Excluded" in text, "DATA_LICENSE.md missing exclusion decision"


def test_privacy_file_has_required_sections() -> None:
    """PRIVACY.md must cover data categories and retention."""
    path = ROOT / "PRIVACY.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    assert "SQLite" in text
    assert "3 days" in text or "3 天" in text
    assert "AI" in text
    assert "Tavily" in text
    assert "deployer" in text
