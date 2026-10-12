"""The small pieces of the first-run setup (crucible#79, #119, #120, #121): the two
stores of the GitHub App credential the service owns (ADR 0017), the configured rule,
and the plain words the gateway test and the Status page use."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.github import credentials as credentials_module
from crucible.adapters.github.credentials import DirectoryAppCredentials, SecretAppCredentials
from crucible.application.admin.credentials import model_ids
from crucible.application.admin.gateway import plain_outcome
from crucible.ports.github import GitHubAppStoreError

# Assembled at run time so no fixture on disk carries a key header the scanner flags.
_LABEL = b"RSA " + b"PRIVATE KEY"
PEM = b"-----BEGIN " + _LABEL + b"-----\nnot-a-real-key\n-----END " + _LABEL + b"-----\n"


def test_the_secret_store_is_created_as_the_services_own_and_read_back() -> None:
    api = FakeKubernetesApi()
    store = SecretAppCredentials(api, name="hades-github-app")
    assert store.read() is None
    assert store.describe()["exists"] is False

    done = store.write(app_id=42, private_key=PEM, webhook_secret=b"hook")

    assert done == {"store": "secret", "name": "hades-github-app", "created": True}
    body = api.objects[("secrets", "hades-github-app")].body
    assert body["metadata"]["labels"] == {
        k8sspec.LABEL_MANAGED_BY: "crucible",
        k8sspec.LABEL_CREDENTIAL: "github-app",
    }
    credential = store.read()
    assert credential is not None and credential.app_id == 42 and credential.private_key == PEM
    assert "not-a-real-key" not in repr(credential)
    described = store.describe()
    assert described["service_owned"] is True and described["webhook_secret_present"] is True


def test_a_second_write_replaces_the_key_and_keeps_a_webhook_secret_not_given() -> None:
    api = FakeKubernetesApi()
    store = SecretAppCredentials(api)
    store.write(app_id=42, private_key=PEM, webhook_secret=b"hook")

    again = store.write(app_id=43, private_key=PEM.replace(b"not", b"new"), webhook_secret=None)

    assert again["created"] is False
    credential = store.read()
    assert credential is not None and credential.app_id == 43
    assert b"new-a-real-key" in credential.private_key
    assert store.describe()["webhook_secret_present"] is True


def test_a_gitops_secret_counts_only_with_the_settings_app_id_and_enabled() -> None:
    """ADR 0017: a Secret a deployment placed itself (no `app-id` key) is configured
    only when the settings name the App and turn GitHub on, as before the ADR."""
    api = FakeKubernetesApi()
    api.create(
        "secrets",
        k8sspec.secret(
            name="hades-github-app", namespace="", object_labels={}, data={"app.pem": PEM}
        ),
    )
    assert SecretAppCredentials(api).read() is None
    assert SecretAppCredentials(api, settings_app_id=7).read() is None
    found = SecretAppCredentials(api, settings_app_id=7, settings_enabled=True).read()
    assert found is not None and found.app_id == 7
    assert SecretAppCredentials(api).describe()["service_owned"] is False


def test_an_unreadable_secret_is_a_refusal_not_an_absence() -> None:
    class Forbidden(FakeKubernetesApi):
        def get(self, kind: str, name: str) -> dict[str, object]:
            raise KubernetesApiError(403, "forbidden")

    store = SecretAppCredentials(Forbidden(), name="hades-github-app")
    with pytest.raises(GitHubAppStoreError, match="not readable"):
        store.read()
    assert store.describe()["exists"] is None


def test_a_secret_of_another_type_under_the_name_is_never_used_or_adopted() -> None:
    """ADR 0017: `create` cannot be narrowed by name, so a service-account token Secret
    could sit under this name; it is refused, not read as the App credential."""
    api = FakeKubernetesApi()
    body = k8sspec.secret(
        name="hades-github-app", namespace="", object_labels={}, data={"app.pem": PEM}
    )
    body["type"] = "kubernetes.io/service-account-token"
    api.create("secrets", body)
    store = SecretAppCredentials(api, settings_app_id=7, settings_enabled=True)
    with pytest.raises(GitHubAppStoreError, match="not Opaque"):
        store.read()
    with pytest.raises(GitHubAppStoreError, match="not Opaque"):
        store.write(app_id=7, private_key=PEM, webhook_secret=None)


def test_the_directory_store_writes_private_files_beside_the_key(tmp_path: Path) -> None:
    directory = tmp_path / "github"
    directory.mkdir(mode=0o700)
    store = DirectoryAppCredentials(
        str(directory / "app.pem"), webhook_secret_path=str(directory / "webhook.secret")
    )
    assert store.read() is None

    store.write(app_id=9, private_key=PEM, webhook_secret=b"hook")

    for name in ("app.pem", "app-id", "webhook.secret"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    credential = store.read()
    assert credential is not None and credential.app_id == 9
    assert store.describe()["service_owned"] is True
    assert not list(directory.glob(".*incoming"))


OTHER_PEM = PEM.replace(b"not-a-real-key", b"another-fake-key")


def _directory_store(tmp_path: Path) -> tuple[Path, DirectoryAppCredentials]:
    directory = tmp_path / "github"
    directory.mkdir(mode=0o700)
    return directory, DirectoryAppCredentials(
        str(directory / "app.pem"), webhook_secret_path=str(directory / "webhook.secret")
    )


def test_a_directory_replacement_switches_the_id_and_the_key_as_one(tmp_path: Path) -> None:
    directory, store = _directory_store(tmp_path)
    store.write(app_id=9, private_key=PEM, webhook_secret=None)
    store.write(app_id=10, private_key=OTHER_PEM, webhook_secret=None)

    credential = store.read()
    assert credential is not None
    assert (credential.app_id, credential.private_key) == (10, OTHER_PEM)
    # The configured key path and the id file follow the version in force.
    assert (directory / "app.pem").is_symlink()
    assert (directory / "app.pem").read_bytes() == OTHER_PEM
    assert (directory / "app-id").read_bytes() == b"10"
    # The version in force and the one before it are kept, nothing older.
    store.write(app_id=11, private_key=PEM, webhook_secret=None)
    assert len(list((directory / ".versions").iterdir())) == 2


def test_a_failure_between_the_two_writes_leaves_the_previous_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, store = _directory_store(tmp_path)
    store.write(app_id=9, private_key=PEM, webhook_secret=None)
    in_force = list((directory / ".versions").iterdir())

    real = credentials_module._write_private

    def fail_on_the_key(target: Path, value: bytes) -> None:
        if target.name == "app.pem":
            raise GitHubAppStoreError(f"{target} could not be written (OSError)")
        real(target, value)

    monkeypatch.setattr(credentials_module, "_write_private", fail_on_the_key)
    with pytest.raises(GitHubAppStoreError, match="could not be written"):
        store.write(app_id=10, private_key=OTHER_PEM, webhook_secret=None)

    credential = store.read()
    assert credential is not None
    assert (credential.app_id, credential.private_key) == (9, PEM)
    assert (directory / "app-id").read_bytes() == b"9"
    assert list((directory / ".versions").iterdir()) == in_force


def test_a_reader_takes_both_files_from_the_version_it_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that lands between a reader's two file reads does not mix the pair."""
    _, store = _directory_store(tmp_path)
    store.write(app_id=9, private_key=PEM, webhook_secret=None)
    real_read = Path.read_bytes
    switched: list[bool] = []

    def read_then_switch(path: Path) -> bytes:
        data = real_read(path)
        if path.name == "app-id" and not switched:
            switched.append(True)
            store.write(app_id=10, private_key=OTHER_PEM, webhook_secret=None)
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_switch)
    credential = store.read()
    assert switched and credential is not None
    assert (credential.app_id, credential.private_key) == (9, PEM)
    monkeypatch.undo()
    after = store.read()
    assert after is not None and (after.app_id, after.private_key) == (10, OTHER_PEM)


def test_a_failure_after_the_switch_is_a_warning_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the pair is switched it is in force, and the caller has already recorded
    it: tidying that fails afterwards is reported, never raised."""
    directory, store = _directory_store(tmp_path)
    store.write(app_id=9, private_key=PEM, webhook_secret=None)
    real = credentials_module._link

    def refuse_the_key_path(target: Path, points_to: Path) -> None:
        if target.name == "app.pem":
            raise GitHubAppStoreError(f"{target} could not be switched (OSError)")
        real(target, points_to)

    (directory / "app.pem").unlink()
    (directory / "app.pem").write_bytes(PEM)  # a key path that is not a link any more
    monkeypatch.setattr(credentials_module, "_link", refuse_the_key_path)
    done = store.write(app_id=10, private_key=OTHER_PEM, webhook_secret=b"hook")

    assert "in force" in done["warning"]
    credential = store.read()
    assert credential is not None
    assert (credential.app_id, credential.private_key) == (10, OTHER_PEM)
    assert (directory / "webhook.secret").read_bytes() == b"hook"


def test_saves_at_the_same_time_never_lose_the_credential(tmp_path: Path) -> None:
    import threading  # noqa: PLC0415

    directory, store = _directory_store(tmp_path)
    store.write(app_id=1, private_key=PEM, webhook_secret=None)
    failures: list[BaseException] = []

    def save(app_id: int) -> None:
        try:
            for _ in range(20):
                store.write(app_id=app_id, private_key=PEM, webhook_secret=None)
        except BaseException as exc:  # reported below
            failures.append(exc)

    threads = [threading.Thread(target=save, args=(n,)) for n in range(2, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    credential = store.read()
    assert credential is not None and credential.app_id in range(2, 6)
    assert (directory / "app.pem").read_bytes() == PEM
    assert len(list((directory / ".versions").iterdir())) == 2


def test_an_api_server_that_refuses_the_connection_is_an_unreadable_row() -> None:
    from crucible.application.admin import credentials as credentials_admin  # noqa: PLC0415

    class Refusing:
        def credential_secret(self, harness: str) -> str:
            return f"hades-harness-{harness}"

        def read_credential_secret(self, harness: str) -> None:
            raise ConnectionRefusedError(111, "Connection refused")

    read = credentials_admin.read_secret(Refusing(), "codex")
    assert read.body is None and read.error is not None
    assert "could not be reached (ConnectionRefusedError)" in read.error


def test_files_placed_by_hand_are_read_until_the_first_write(tmp_path: Path) -> None:
    directory, store = _directory_store(tmp_path)
    (directory / "app.pem").write_bytes(PEM)
    (directory / "app-id").write_bytes(b"9")
    credential = store.read()
    assert credential is not None and credential.app_id == 9

    store.write(app_id=10, private_key=OTHER_PEM, webhook_secret=None)
    credential = store.read()
    assert credential is not None
    assert (credential.app_id, credential.private_key) == (10, OTHER_PEM)
    assert (directory / "app.pem").read_bytes() == OTHER_PEM


def test_a_read_only_directory_is_refused_by_name(tmp_path: Path) -> None:
    directory = tmp_path / "github"
    directory.mkdir(mode=0o500)
    store = DirectoryAppCredentials(str(directory / "app.pem"))
    try:
        with pytest.raises(GitHubAppStoreError, match="read-only"):
            store.write(app_id=9, private_key=PEM, webhook_secret=None)
    finally:
        directory.chmod(0o700)


@pytest.mark.parametrize(
    ("outcome", "words"),
    [
        (None, "never tested"),
        ("probe:completed", "the last test passed"),
        ("probe:auth_failure", "the key was not accepted"),
        ("probe:inconclusive:endpoint_not_configured", "the gateway URL is not set"),
        ("probe:inconclusive:readiness_http_503", "readiness check answered HTTP 503"),
        ("probe:inconclusive:models_http_500", "listing the gateway's models answered HTTP 500"),
        ("completed", "the last launch ended completed"),
    ],
)
def test_a_recorded_outcome_reads_as_plain_words(outcome: str | None, words: str) -> None:
    assert words in plain_outcome(outcome)


def test_model_ids_read_an_openai_list_and_nothing_else() -> None:
    assert model_ids(b'{"data": [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"x": 1}]}') == ["a", "b"]
    assert model_ids(b"not json") == []
    assert model_ids(b'{"data": "nope"}') == []


def test_status_waits_a_bounded_time_for_a_slow_app_credential_store() -> None:
    import asyncio  # noqa: PLC0415
    import threading  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    from crucible.application.admin import github as github_admin  # noqa: PLC0415

    release = threading.Event()

    class Stalled:
        name = "hades-github-app"
        namespace = "crucible"

        def describe(self) -> dict[str, object]:
            release.wait(30)
            return {}

        def read(self) -> None:
            return None

    async def scenario() -> github_admin.Stored:
        try:
            return await github_admin.read_stored(
                SimpleNamespace(github_credentials=Stalled()),  # type: ignore[arg-type]
                timeout=0.2,
            )
        finally:
            release.set()

    described, credential = asyncio.run(scenario())
    assert credential is None and described is not None
    assert described["exists"] is None and described["name"] == "hades-github-app"
    assert "did not answer within 0.2 seconds" in str(described["detail"])
