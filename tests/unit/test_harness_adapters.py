"""Launch-spec construction per adapter (07, 18): the exact flags, the prompt on stdin
as a pointer, no secret anywhere in argv or env, and the credential spec each declares.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from typing import Any

import pytest

from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.hermes import HermesAdapter
from crucible.adapters.harness.registry import default_adapters, default_registry
from crucible.adapters.harness.script import ScriptHarnessAdapter
from crucible.application.harnesses import effective_mount_mode
from crucible.domain.entities import HarnessState
from crucible.domain.secrets import scan_text
from crucible.ports.harness import (
    CredentialSource,
    HarnessAdapter,
    HarnessGate,
    HarnessUnavailableError,
    LaunchContext,
    MountMode,
)
from tests.fixtures import FakeClock

POINTER = "Read /crucible/identity/IDENTITY.md and execute the task."


def context(**overrides: Any) -> LaunchContext:
    base: dict[str, Any] = {
        "attempt_id": "01ATTEMPT0000000000000000A",
        "model": "model-x",
        "effort": None,
        "timeout_seconds": 900,
        "identity_mount": "/crucible/identity",
        "report_mount": "/crucible/report",
        "repo_mount": "/crucible/repo",
        "credential_mounted": True,
    }
    base.update(overrides)
    return LaunchContext(**base)


def test_claude_code_launch_matches_07() -> None:
    launch = ClaudeCodeAdapter().build_launch(context())
    assert launch.argv == (
        "/usr/local/bin/claude",
        "-p",
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt-file",
        "/crucible/identity/IDENTITY.md",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "model-x",
    )
    assert launch.stdin_text == POINTER and launch.stdin_files == ()
    assert launch.env == {
        "CLAUDE_CONFIG_DIR": "/home/worker/.claude",
        # Issue 128: no auto-backgrounding, and the Bash timeouts from the launch. With
        # no value on the context, the default of 60 minutes is capped at the attempt's
        # 900 seconds.
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "BASH_DEFAULT_TIMEOUT_MS": "900000",
        "BASH_MAX_TIMEOUT_MS": "900000",
    }
    # The one exception to file-only delivery (07): a name and a path, never a value.
    assert launch.env_from_files == {"CLAUDE_CODE_OAUTH_TOKEN": "/home/worker/.claude/oauth-token"}
    assert launch.transcript_path == "/crucible/report/transcript.jsonl"


def test_codex_launch_matches_07_with_s1_and_s6_flags() -> None:
    launch = CodexAdapter().build_launch(context(effort="low"))
    argv = launch.argv
    assert argv[:2] == ("/usr/local/bin/codex", "exec")
    assert "--dangerously-bypass-approvals-and-sandbox" in argv  # S2: the container is the boundary
    assert "--skip-git-repo-check" in argv  # S1: the checkout is not a trusted host uid's
    assert argv[argv.index("--disable") + 1] == "plugins"  # S6
    assert "check_for_update_on_startup=false" in argv  # S7, S11
    assert argv[argv.index("-o") + 1] == "/crucible/report/codex-last-message.md"
    assert argv[argv.index("--model") + 1] == "model-x"
    assert argv[argv.index("-C") + 1] == "/crucible/repo"
    assert 'model_reasoning_effort="low"' in argv
    # IDENTITY.md followed by the prompt on stdin (07).
    assert launch.stdin_files == ("/crucible/identity/IDENTITY.md",)
    assert launch.stdin_text == POINTER
    assert launch.env == {"CODEX_HOME": "/home/worker/.codex"}
    assert launch.env_from_files == {}
    # Codex never gets a sandbox flag (S2).
    assert "--sandbox" not in argv


def test_agy_launch_matches_07_and_stays_under_the_argv_ceiling() -> None:
    launch = AgyAdapter().build_launch(context(effort="low", timeout_seconds=1200))
    argv = launch.argv
    assert argv[:2] == ("/usr/local/bin/agy", "-p") and argv[2].startswith(POINTER + " ")
    assert argv[argv.index("--model") + 1] == "model-x"
    assert argv[argv.index("--effort") + 1] == "low"
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--add-dir") + 1] == "/crucible/identity"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    # The CLI's own print timeout defaults to five minutes; it follows the attempt's.
    assert argv[argv.index("--print-timeout") + 1] == "1200s"
    assert all(len(arg) < 1024 for arg in argv), "S3: argv carries only a short pointer"
    assert launch.stdin_text == "" and launch.stdin_files == ()
    assert launch.env == {} and launch.env_from_files == {}


def test_hermes_launch_matches_07_and_uses_the_optional_api_key() -> None:
    adapter = HermesAdapter()
    launch = adapter.build_launch(
        context(
            model="coder",
            credential_mounted=True,
            endpoint="local",
            endpoint_url="http://spark.example.internal:11434/v1",
        )
    )
    assert launch.argv == (
        "/usr/local/bin/crucible-hermes",
        "--ignore-user-config",
        "--ignore-rules",
        "--safe-mode",
        "--yolo",
        "--provider",
        "openai-api",
        "--model",
        "coder",
        "--toolsets",
        "terminal,file",
        "--usage-file",
        "/crucible/report/hermes-usage.json",
        "-z",
        POINTER,
    )
    assert launch.env == {
        # FDY-0140: no PATH, so the model's commands never resolve into the Hermes venv.
        "HERMES_HOME": "/home/worker/.hermes",
        "CRUCIBLE_HERMES_IDENTITY": "/crucible/identity/IDENTITY.md",
        "CRUCIBLE_HERMES_MAX_TURNS": "300",
        "CRUCIBLE_HERMES_CONTEXT_LENGTH": "131072",
        # Hades #388: the gateway's response allowance and the entry's thinking setting.
        "CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS": "32000",
        "CRUCIBLE_HERMES_THINKING": "false",
        "OPENAI_BASE_URL": "http://spark.example.internal:11434/v1",
        "OPENAI_API_KEY": "local-no-auth",
        "CRUCIBLE_HERMES_USAGE": "/crucible/report/hermes-usage.json",
        # Issue 128: whole seconds, from the launch's command timeout.
        "TERMINAL_TIMEOUT": "900",
        "TERMINAL_MAX_FOREGROUND_TIMEOUT": "900",
        "CRUCIBLE_IN_FLIGHT_FILE": "/home/worker/.hermes/processes.json",
    }
    assert launch.env_from_files == {"OPENAI_API_KEY": "/home/worker/.hermes-auth/api-key"}
    assert launch.stdin_files == () and launch.stdin_text == ""
    assert launch.transcript_path == "/crucible/report/transcript.jsonl"
    assert launch.workdir == "/crucible/repo"
    credential = adapter.credential_spec()
    assert credential is not None
    assert credential.minimum_mode is MountMode.RO
    assert not credential.required_for_launch
    assert [(item.name, item.sync_back) for item in credential.auth_files] == [("api-key", False)]


def test_hermes_run_limits_come_from_the_saved_setting() -> None:
    """FDY-0140: the Local gateway page's limits reach the launch."""
    launch = HermesAdapter().build_launch(
        dataclasses.replace(
            context(model="coder", endpoint="local", endpoint_url="http://gw.invalid/v1"),
            harness_settings={"max_turns": 450, "context_length": 0},
        )
    )
    assert launch.env["CRUCIBLE_HERMES_MAX_TURNS"] == "450"
    assert launch.env["CRUCIBLE_HERMES_CONTEXT_LENGTH"] == "0"
    assert "PATH" not in launch.env


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "gpt-oss:120b", "endpoint": "subscription", "endpoint_url": None},
    ],
)
def test_hermes_refuses_a_subscription_launch_with_no_endpoint_url(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        HermesAdapter().build_launch(context(credential_mounted=False, **overrides))


def test_hermes_without_a_key_uses_the_explicit_no_auth_fallback() -> None:
    launch = HermesAdapter().build_launch(
        context(
            model="coder",
            credential_mounted=False,
            endpoint="local",
            endpoint_url="https://llm.apps.int.sentania.net/v1",
        )
    )
    assert launch.env["OPENAI_API_KEY"] == "local-no-auth"
    assert launch.env_from_files == {}


def test_script_harness_is_a_plain_argv() -> None:
    launch = ScriptHarnessAdapter().build_launch(context(credential_mounted=False))
    assert launch.argv == ("crucible-script-harness",)
    assert launch.transcript_path is None and launch.stdin_text == ""
    assert ScriptHarnessAdapter().credential_spec() is None


@pytest.mark.parametrize("adapter", default_adapters(test_fixtures=True), ids=lambda a: a.name)
def test_nothing_secret_shaped_in_any_launch(adapter: HarnessAdapter) -> None:
    """12: argv and env carry names, paths and flags; the scanner finds nothing."""
    ctx = (
        context(
            model="gpt-oss:120b",
            credential_mounted=False,
            endpoint="local",
            endpoint_url="http://spark.example.internal:11434/v1",
        )
        if adapter.name in {"hermes", "qwen_code"}
        else context()
    )
    launch = adapter.build_launch(ctx)
    blob = "\n".join([*launch.argv, *launch.env.values(), *launch.env_from_files.values()])
    assert scan_text(blob) is None
    for value in launch.env_from_files.values():
        assert value.startswith("/home/worker/"), "a path inside the mounted copy, never a value"


def test_without_a_credential_mounted_no_config_env_is_set() -> None:
    for adapter in (ClaudeCodeAdapter(), CodexAdapter()):
        launch = adapter.build_launch(context(credential_mounted=False))
        assert "CLAUDE_CONFIG_DIR" not in launch.env and "CODEX_HOME" not in launch.env
        assert launch.env_from_files == {}


# ----- credential specs (12) --------------------------------------------------


def test_credential_specs_name_only_the_auth_files_s1_recorded() -> None:
    claude = ClaudeCodeAdapter().credential_spec()
    assert [f.name for f in claude.auth_files] == ["oauth-token", ".claude.json"]
    assert claude.minimum_mode is MountMode.RO
    assert effective_mount_mode(claude, None) is MountMode.RO
    assert claude.config_dir_env == "CLAUDE_CONFIG_DIR"
    assert set(claude.templates) == {"settings.json"}
    # Neither file is written back: the long-lived token never refreshes (S1b) and the
    # state file is state.
    assert all(not f.sync_back for f in claude.auth_files)

    codex = CodexAdapter().credential_spec()
    assert [f.name for f in codex.auth_files] == ["auth.json"]
    assert codex.minimum_mode is MountMode.RENEWER
    assert codex.auth_files[0].issued_at == ("last_refresh",)
    assert codex.auth_files[0].json and codex.auth_files[0].sync_back
    assert set(codex.templates) == {"config.toml"}

    agy = AgyAdapter().credential_spec()
    assert [f.name for f in agy.auth_files] == ["antigravity-cli/antigravity-oauth-token"]
    assert agy.minimum_mode is MountMode.RW_NARROW
    assert agy.source_subdir == ".gemini" and agy.mount_target == "/home/worker/.gemini"
    assert agy.auth_files[0].issued_at == ("token", "expiry")


def test_configuration_may_raise_the_mount_mode_and_never_lower_it() -> None:
    """25 step 7."""
    ro_minimum = replace(AgyAdapter().credential_spec(), minimum_mode=MountMode.RO)
    assert effective_mount_mode(ro_minimum, None) is MountMode.RO
    assert effective_mount_mode(ro_minimum, CredentialSource("/x")) is MountMode.RO
    assert effective_mount_mode(ro_minimum, CredentialSource("/x", MountMode.RW_NARROW)) is (
        MountMode.RW_NARROW
    )
    codex = CodexAdapter().credential_spec()
    assert effective_mount_mode(codex, CredentialSource("/x", MountMode.RO)) is MountMode.RENEWER


# ----- the registry (07, 25) ----------------------------------------------------


def test_the_registry_knows_the_four_harnesses_and_the_fixture_only_when_asked() -> None:
    """crucible#124: the script harness is a test fixture, off unless a tier enables it."""
    assert default_registry().names() == ("claude_code", "codex", "agy", "hermes", "qwen_code")
    registry = default_registry(test_fixtures=True)
    assert registry.names() == (
        "claude_code",
        "codex",
        "agy",
        "hermes",
        "qwen_code",
        "script-harness",
    )
    # C11: 0.154, 0.155 and 0.156 keep every flag the launch uses (codex exec --help of
    # 0.156.0; their changelogs remove only `codex mcp-server`, which is not used).
    assert registry.require("codex").supported_versions.text == ">=0.153.0,<0.157.0"
    assert registry.require("claude_code").supported_versions.text == ">=2.1.277,<2.2.0"
    assert registry.require("agy").supported_versions.text == ">=1.2.0,<1.3.0"
    assert registry.require("hermes").supported_versions.text == ">=0.19.0,<0.20.0"


def test_an_unknown_name_is_refused() -> None:
    with pytest.raises(HarnessUnavailableError, match="no adapter declares"):
        default_registry().resolve("nonesuch")


def test_a_configuration_gate_refuses_with_its_reason() -> None:
    gates = {"codex": HarnessGate(enabled=False, reason="unverified (S1b)")}
    with pytest.raises(HarnessUnavailableError, match="configuration default: unverified"):
        default_registry().resolve("codex", gates=gates)
    # Another harness is unaffected by a gate that does not name it.
    assert default_registry().resolve("claude_code", gates=gates).name == "claude_code"


def test_an_administrators_decision_replaces_the_configuration_default() -> None:
    """hades #174: the configuration is the starting value. A row nobody decided keeps
    the old rule (both must say yes); an administrator's enable or disable decides."""
    gates = {"codex": HarnessGate(enabled=False, reason="unverified (S1b)")}
    state = HarnessState(
        name="codex",
        enabled=True,
        reason="",
        session_compatibility="unverified",
        updated_at=FakeClock().now(),
        updated_by="crucible",
    )
    with pytest.raises(HarnessUnavailableError, match="configuration default"):
        default_registry().resolve("codex", gates=gates, state=state)
    state.enabled_decided = True
    assert default_registry().resolve("codex", gates=gates, state=state).name == "codex"
    state.enabled = False
    with pytest.raises(HarnessUnavailableError, match="disabled by an administrator"):
        default_registry().resolve("codex", gates={}, state=state)


def test_an_administrator_flag_refuses_with_its_reason() -> None:
    state = HarnessState(
        name="agy",
        enabled=False,
        reason="credential rotated; pending probe",
        session_compatibility="unverified",
        updated_at=FakeClock().now(),
        updated_by="admin",
    )
    with pytest.raises(HarnessUnavailableError, match="disabled by an administrator"):
        default_registry().resolve("agy", state=state)
    state.enabled = True
    assert default_registry().resolve("agy", state=state).name == "agy"


def test_the_fixture_harness_makes_one_model_call_when_tested_behind_a_local_endpoint() -> None:
    """crucible#118: the harness test's model call, from the worker and through its
    egress, against the endpoint the routing policy names. Nothing to call otherwise."""
    adapter = ScriptHarnessAdapter()

    def context(**kwargs: Any) -> LaunchContext:
        return LaunchContext(
            attempt_id="probe",
            model="stub-model",
            effort=None,
            timeout_seconds=60,
            identity_mount="/crucible/identity",
            report_mount="/crucible/report",
            repo_mount="/crucible/repo",
            probe=True,
            **kwargs,
        )

    local = adapter.build_launch(
        context(endpoint="local", endpoint_url="https://stub.example.invalid/v1")
    )
    assert local.argv[:2] == ("sh", "-c") and "chat/completions" in local.argv[2]
    assert local.env["CRUCIBLE_TEST_ENDPOINT"] == "https://stub.example.invalid/v1"
    assert '"model": "stub-model"' in local.env["CRUCIBLE_TEST_BODY"]
    assert adapter.build_launch(context()).argv == ("sh", "-c", "exit 0")
