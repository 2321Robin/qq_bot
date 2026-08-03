"""Container configuration contract tests (S1-CTR-01..04, S1-HEALTH)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"tvly-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(api[_-]?key|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9]{16,}"),
)


def test_dockerfile_runs_as_non_root_user() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER" in text
    assert not re.search(r"USER\s+root\b", text)
    assert not re.search(r"USER\s+0\b", text)


def test_dockerfile_has_stdlib_healthcheck() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in text
    assert "/healthz" in text
    # the healthcheck must use the Python standard library, not curl/wget
    assert "urllib.request" in text
    assert "CMD [" in text
    assert "CMD curl" not in text
    assert '"curl"' not in text
    assert '"wget"' not in text


def test_dockerfile_does_not_copy_secrets_or_local_data() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert not re.search(r"COPY\b.*\.env\b", text)
    assert not re.search(r"COPY\b.*(chat_memory|data)", text)
    assert "chat_memory_path" not in text


def test_dockerfile_exposes_health_endpoint_and_configurable_port() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "HOST" in text
    assert "PORT" in text


def test_compose_has_no_napcat_service_or_private_data() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"backend"}
    assert "napcat" not in (ROOT / ".dockerignore").read_text(encoding="utf-8")


def test_compose_contains_no_secret_literals() -> None:
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    for pattern in _SECRET_PATTERNS:
        assert not pattern.search(text), f"secret-like literal in compose.yaml: {pattern.pattern}"


def test_compose_backend_service_shape() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert "backend" in services
    backend = services["backend"]
    assert "healthcheck" in backend
    assert "env_file" in backend
    # data volume must be declared and mounted
    volumes = compose.get("volumes", {})
    assert "qq-bot-data" in volumes
    mounted = " ".join(backend.get("volumes", []))
    assert "qq-bot-data" in mounted
    assert "/app/data" in mounted


def test_compose_backend_port_is_configurable() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    backend = compose["services"]["backend"]
    assert "PORT" in str(backend.get("environment", {})) or "PORT" in str(backend.get("ports", []))


def test_dockerignore_excludes_sensitive_paths() -> None:
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for entry in (".git", ".env", ".venv", "data", "tests", "scripts", "docs"):
        assert entry in text, f".dockerignore missing entry {entry!r}"


def test_readyz_is_ok_without_private_data_or_onebot(tmp_path, monkeypatch) -> None:
    """Container smoke contract (S4-HEALTH-06, S1-CTR-04): an empty data
    volume and no OneBot connection must still yield /readyz 200."""
    import asyncio

    from qq_bot import runtime as runtime_module
    from qq_bot.plugins import health as health_module
    from qq_bot.plugins.health import _response_body, readyz
    from qq_bot.services.chat_memory import ChatMemoryRepository

    async def scenario() -> None:
        repository = ChatMemoryRepository(tmp_path / "chat.sqlite3", retention_days=30)
        await repository.open()
        try:

            class ReadyRuntime:
                def is_ready(self) -> bool:
                    return True

                def get_chat_repository(self) -> ChatMemoryRepository:
                    return repository

            monkeypatch.setattr(runtime_module, "get_runtime", lambda: ReadyRuntime())
            health_module._details_dir = lambda: tmp_path / "missing_details"  # type: ignore[assignment]
            response = await readyz()
            body = _response_body(response)
            assert response.status_code == 200
            assert body["status"] == "ready"
            assert body["checks"] == {
                "database": "ok",
                "data_version": "ok",
                "onebot": "disconnected",
            }
        finally:
            await repository.close()

    asyncio.run(scenario())


def test_metrics_uses_existing_port_no_new_expose() -> None:
    """S4-METRIC-11: /metrics is served on the same port as /healthz; the
    container must not open additional ports."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    exposes = re.findall(r"EXPOSE\s+(\S+)", dockerfile)
    assert exposes == ["8081"], f"unexpected EXPOSE set: {exposes}"
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    ports = str(compose["services"]["backend"].get("ports", []))
    assert "8081" in ports
    assert "metrics" not in (ROOT / "compose.yaml").read_text(encoding="utf-8").lower()
