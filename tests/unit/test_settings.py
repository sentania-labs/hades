from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from crucible.cli.wiring import kubernetes_config
from crucible.domain.cluster_egress import ClusterEgress
from crucible.settings import Settings, load_settings


def test_defaults_without_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRUCIBLE_CONFIG", raising=False)
    monkeypatch.delenv("CRUCIBLE_DATABASE__URL", raising=False)
    settings = load_settings()
    assert settings.database.url.startswith("postgresql+psycopg://crucible:CHANGE_ME@localhost")
    assert settings.supervisor.tick_seconds == 5.0


def test_toml_values_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRUCIBLE_DATABASE__URL", raising=False)
    cfg = tmp_path / "crucible.toml"
    cfg.write_text(
        '[database]\nurl = "postgresql+psycopg://file:CHANGE_ME@filehost:5432/crucible"\n'
        "[supervisor]\ntick_seconds = 2\n[docker]\nendpoint = 'ignored-in-c1'\n"
    )
    settings = load_settings(str(cfg))
    assert settings.database.url == "postgresql+psycopg://file:CHANGE_ME@filehost:5432/crucible"
    assert settings.supervisor.tick_seconds == 2.0


def test_environment_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "crucible.toml"
    cfg.write_text(
        '[database]\nurl = "postgresql+psycopg://file:CHANGE_ME@filehost:5432/crucible"\n'
        "[supervisor]\ntick_seconds = 2\nlease_ttl_seconds = 45\n"
    )
    monkeypatch.setenv(
        "CRUCIBLE_DATABASE__URL", "postgresql+psycopg://env:CHANGE_ME@envhost:5432/crucible"
    )
    monkeypatch.setenv("CRUCIBLE_SUPERVISOR__TICK_SECONDS", "1")
    settings = load_settings(str(cfg))
    assert settings.database.url == "postgresql+psycopg://env:CHANGE_ME@envhost:5432/crucible"
    assert settings.supervisor.tick_seconds == 1.0
    assert settings.supervisor.lease_ttl_seconds == 45, "file values not overridden still apply"


def test_config_env_var_names_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[service]\nbind = "127.0.0.1:9090"\n')
    monkeypatch.setenv("CRUCIBLE_CONFIG", str(cfg))
    monkeypatch.delenv("CRUCIBLE_SERVICE__BIND", raising=False)
    assert load_settings().service.port == 9090


def test_credentials_and_harness_gates_from_toml_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """12 and 25: a credential is a path and a mount mode, a harness gate is a flag with
    its reason; both come from the file and either can be overridden from the environment.
    Nothing here is a value."""
    cfg = tmp_path / "crucible.toml"
    cfg.write_text(
        "[credentials.codex]\n"
        'path = "/var/lib/crucible/credentials/codex"\n'
        'mount_mode = "rw-narrow"\n'
        "[credentials.agy]\n"
        'path = "/var/lib/crucible/credentials/agy"\n'
        "[harnesses.codex]\n"
        "enabled = false\n"
        'reason = "unverified (S1b)"\n'
    )
    monkeypatch.setenv("CRUCIBLE_CREDENTIALS__AGY__MOUNT_MODE", "rw-narrow")
    monkeypatch.setenv("CRUCIBLE_HARNESSES__AGY__ENABLED", "false")
    monkeypatch.setenv("CRUCIBLE_HARNESSES__AGY__REASON", "probe pending")
    settings = load_settings(str(cfg))
    assert settings.credentials["codex"].path == "/var/lib/crucible/credentials/codex"
    assert settings.credentials["codex"].mount_mode == "rw-narrow"
    assert settings.credentials["agy"].mount_mode == "rw-narrow", "the environment overrides"
    assert settings.harnesses["codex"].enabled is False
    assert settings.harnesses["codex"].reason == "unverified (S1b)"
    assert settings.harnesses["agy"].enabled is False
    assert settings.harnesses["agy"].reason == "probe pending"
    # A harness with no entry is not gated by configuration.
    assert "claude_code" not in settings.harnesses


def test_the_probe_image_is_its_own_field_and_not_a_repository() -> None:
    """26's readiness canary runs an exact reference a deployment names, and a bare
    repository means `:latest` to a kubelet. Keeping it out of `image_repositories` is
    what stops `list_images` taking it for a repository and paying a suppressed registry
    round trip per tag on every admin images read."""
    settings = Settings(
        kubernetes={
            "enabled": True,
            "image_repositories": ["registry.example/crucible-worker"],
            "probe_image": "registry.example/crucible-worker:script-harness-1.0.0",
        }
    )
    config = kubernetes_config(settings)
    assert config.image_repositories == ("registry.example/crucible-worker",)
    assert config.probe_image == "registry.example/crucible-worker:script-harness-1.0.0"


def test_without_a_probe_image_the_field_is_empty() -> None:
    settings = Settings(kubernetes={"image_repositories": ["registry.example/w"]})
    config = kubernetes_config(settings)
    assert config.image_repositories == ("registry.example/w",)
    assert config.probe_image == ""


def test_the_egress_selectors_default_to_coredns_and_no_in_cluster_endpoint() -> None:
    config = kubernetes_config(Settings())
    assert config.egress == ClusterEgress()
    assert config.control_namespace == "hades"


def test_the_egress_selectors_seed_from_the_settings_file() -> None:
    settings = Settings(
        kubernetes={
            "local_endpoint_namespace": "litellm",
            "local_endpoint_pod_labels": {"app.kubernetes.io/name": "litellm"},
            "local_endpoint_port": 4000,
        }
    )
    config = kubernetes_config(settings, local_endpoint_url="http://litellm.litellm.svc:4000")
    assert config.egress.endpoint_namespace == "litellm"
    assert config.egress.endpoint_pod_labels == (("app.kubernetes.io/name", "litellm"),)
    assert config.egress.endpoint_port == 4000
    assert config.local_endpoint_url == "http://litellm.litellm.svc:4000"


def test_a_seed_into_the_workers_namespace_stops_the_service_at_start() -> None:
    settings = Settings(
        kubernetes={
            "local_endpoint_namespace": "hades-workers",
            "local_endpoint_pod_labels": {"a": "b"},
        }
    )
    with pytest.raises(ValueError, match="may never reach"):
        kubernetes_config(settings)


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_pod_pid_limit_override_is_refused(value: int) -> None:
    """-1 is the kubelet's own "PID limiting disabled"; copying it from a node config
    here would make the readiness gate pass with no pod-level limit in force (95's
    Codex correction)."""
    with pytest.raises(ValidationError, match="positive integer"):
        Settings(kubernetes={"pod_pid_limit_override": value})


def test_a_positive_pod_pid_limit_override_is_accepted() -> None:
    settings = Settings(kubernetes={"pod_pid_limit_override": 512})
    assert settings.kubernetes.pod_pid_limit_override == 512


def test_test_fixtures_are_off_unless_turned_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """crucible#124: a production deployment never gets the fake provider or the script
    harness; a test tier turns them on through the environment."""
    monkeypatch.delenv("CRUCIBLE_TEST_FIXTURES", raising=False)
    assert load_settings().test_fixtures is False
    monkeypatch.setenv("CRUCIBLE_TEST_FIXTURES", "true")
    assert load_settings().test_fixtures is True


def test_broad_egress_and_the_resolve_ttl_reach_the_provider() -> None:
    """Issue 61: both are deployment settings, not provider-only fields."""
    config = kubernetes_config(Settings())
    assert config.broad_egress is False
    assert config.resolve_ttl_seconds == 300.0
    config = kubernetes_config(
        Settings(kubernetes={"broad_egress": True, "resolve_ttl_seconds": 45})
    )
    assert config.broad_egress is True
    assert config.resolve_ttl_seconds == 45.0


def test_broad_egress_and_the_resolve_ttl_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cluster deployment sets every Kubernetes setting as an environment variable."""
    monkeypatch.delenv("CRUCIBLE_CONFIG", raising=False)
    monkeypatch.setenv("CRUCIBLE_KUBERNETES__BROAD_EGRESS", "true")
    monkeypatch.setenv("CRUCIBLE_KUBERNETES__RESOLVE_TTL_SECONDS", "120")
    settings = load_settings()
    assert settings.kubernetes.broad_egress is True
    assert settings.kubernetes.resolve_ttl_seconds == 120.0


@pytest.mark.parametrize("value", [0, -5])
def test_a_non_positive_resolve_ttl_is_refused(value: float) -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        Settings(kubernetes={"resolve_ttl_seconds": value})
