"""Plain, actionable test failure titles for issue #171.

Every failing harness test step leads with a sentence the operator can act on,
and the policy internals live under detail.  This test exercises the title
logic in isolation (no database required).

On main, _Steps.failed has no title keyword, so tests that assert the presence
of "title" in a failed step dict will fail.  After the change they pass.
"""

from crucible.application.admin import harness_test

# Expected plain titles (used across tests to avoid long literal lines).
_EXP_HERMES_NO_MODEL = "hermes has no enabled model. Pick one on Local gateway."
_EXP_CODEX_NO_MODEL = (
    "codex has no enabled model in the routing policy in force. "
    "Enable one on Routing."
)  # fmt: skip


class TestStepsFailedTitle:
    """_Steps.failed carries a title on every failure when callers pass it."""

    def test_enabled_step_failure_has_title(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.ENABLED,
            "enabled in configuration but not on Harnesses",
            title="Enable the harness on Harnesses.",
        )
        assert len(steps.items) == 6
        first = steps.items[0]
        assert first["ok"] is False
        assert first["result"] == "fail"
        assert first["title"] == "Enable the harness on Harnesses."

    def test_image_promoted_title(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.IMAGE,
            "no worker image is promoted for hermes; choose one on Images",
            title="Promote a worker image for hermes on Images.",
        )
        assert steps.items[0]["title"] == "Promote a worker image for hermes on Images."

    def test_image_unsupported_title(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.IMAGE,
            "sha256:abc123 carries hermes 99.0 outside the tested range 0.19..0.19; "
            "promote a supported image on Images",
            title="Promote a supported image for hermes on Images.",
        )
        assert steps.items[0]["title"] == "Promote a supported image for hermes on Images."

    def test_credential_absent_hermes(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.CREDENTIAL,
            "no API key is stored for hermes; set it with the gateway URL on Local gateway",
            title="No key is stored for hermes. Set it on Local gateway.",
        )
        assert steps.items[0]["title"] == ("No key is stored for hermes. Set it on Local gateway.")

    def test_credential_absent_other(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.CREDENTIAL,
            "no credential is stored for codex; log in on Credentials",
            title="No credential is stored for codex. Log in on Credentials.",
        )
        assert steps.items[0]["title"] == (
            "No credential is stored for codex. Log in on Credentials."
        )

    def test_credential_unreadable_hermes(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.CREDENTIAL,
            "the credential of hermes cannot be read: key not found",
            title="The credential of hermes cannot be read. Check Local gateway.",
        )
        assert steps.items[0]["title"] == (
            "The credential of hermes cannot be read. Check Local gateway."
        )

    def test_credential_unreadable_other(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.CREDENTIAL,
            "the credential of codex cannot be read: token expired",
            title="The credential of codex cannot be read. Check Credentials.",
        )
        assert steps.items[0]["title"] == (
            "The credential of codex cannot be read. Check Credentials."
        )

    def test_route_hermes_title(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.ROUTE,
            "routing policy hermes, which the policy in force names, has no enabled "
            "model for harness 'hermes'; the probe needs one (enable a model for it, "
            "or put a policy in force that names a routing policy with one)",
            title="hermes has no enabled model. Pick one on Local gateway.",
        )
        assert steps.items[0]["title"] == _EXP_HERMES_NO_MODEL

    def test_route_other_title(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.ROUTE,
            "routing policy codex, which the policy in force names, has no enabled "
            "model for harness 'codex'; the probe needs one (enable a model for it, "
            "or put a policy in force that names a routing policy with one)",
            title=_EXP_CODEX_NO_MODEL,
        )
        assert steps.items[0]["title"] == _EXP_CODEX_NO_MODEL

    def test_worker_error_has_title(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.WORKER,
            "the worker did not start: container creation failed",
            title="Check the worker's egress for hermes.",
        )
        assert steps.items[0]["title"] == "Check the worker's egress for hermes."

    def test_model_call_error_has_title(self) -> None:
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.MODEL,
            "the run ended as provider_unavailable (exit code 42), after 5 s",
            title="Fix the credential or provider for hermes.",
        )
        assert steps.items[0]["title"] == "Fix the credential or provider for hermes."

    def test_passed_step_has_no_title(self) -> None:
        steps = harness_test._Steps([])
        steps.passed(harness_test.CREDENTIAL, "stored (valid)")
        assert "title" not in steps.items[0]

    def test_failed_step_without_title_keyword(self) -> None:
        """On main, passing title= would raise TypeError; with the change it is accepted."""
        steps = harness_test._Steps([])
        steps.failed(
            harness_test.IMAGE,
            "no worker image is promoted for hermes",
            title="Promote a worker image for hermes on Images.",
        )
        assert steps.items[0]["title"] == "Promote a worker image for hermes on Images."


class TestHelperFunctions:
    """_route_title and _credential_title helpers produce plain sentences."""

    def test_is_hermes_like_hermes(self) -> None:
        assert harness_test._is_hermes_like("hermes") is True

    def test_is_hermes_like_qwen(self) -> None:
        assert harness_test._is_hermes_like("qwen_code") is True

    def test_is_hermes_like_codex(self) -> None:
        assert harness_test._is_hermes_like("codex") is False

    def test_route_title_hermes(self) -> None:
        assert harness_test._route_title("hermes", "routing policy ... has no enabled model") == (
            "hermes has no enabled model. Pick one on Local gateway."
        )

    def test_route_title_other(self) -> None:
        assert harness_test._route_title("codex", "routing policy ...") == _EXP_CODEX_NO_MODEL

    def test_credential_title_absent_hermes(self) -> None:
        assert (
            harness_test._credential_title(
                "hermes",
                "no API key is stored for hermes; set it with the gateway URL on Local gateway",
            )
            == "No key is stored for hermes. Set it on Local gateway."
        )

    def test_credential_title_absent_other(self) -> None:
        assert (
            harness_test._credential_title(
                "codex",
                "no credential is stored for codex; log in on Credentials",
            )
            == "No credential is stored for codex. Log in on Credentials."
        )

    def test_credential_title_read_error_hermes(self) -> None:
        assert (
            harness_test._credential_title(
                "hermes",
                "the credential of hermes cannot be read: key not found",
            )
            == "The credential of hermes cannot be read. Check Local gateway."
        )

    def test_credential_title_read_error_other(self) -> None:
        assert (
            harness_test._credential_title(
                "codex",
                "the credential of codex cannot be read: token expired",
            )
            == "The credential of codex cannot be read. Check Credentials."
        )

    def test_credential_title_unknown(self) -> None:
        assert (
            harness_test._credential_title("hermes", "some unknown error") == "some unknown error"
        )
