"""Shared test fixtures (S1-TST-01: tests never read the developer's real .env)."""

from __future__ import annotations

import pytest

from qq_bot.config import BotSettings, get_settings


@pytest.fixture(autouse=True)
def _hermetic_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shadow every settings field with its declared default.

    pydantic-settings prefers OS env vars over the .env file, so setting each
    field to its default value neutralizes the developer's untracked .env
    (real keys, accounts, secrets) without changing documented behavior.
    Empty-string overrides would corrupt behavior — e.g. AI_PREFIX="" would
    disable the "ai" command prefix.
    """
    for name, field in BotSettings.model_fields.items():
        default = field.default
        if isinstance(default, (str, bool, int, float)):
            monkeypatch.setenv(name.upper(), str(default))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
