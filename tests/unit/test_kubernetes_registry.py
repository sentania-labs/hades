"""The crane registry adapter (26, 108) against a stub `crane`.

The stub records what it was run with (argv, the DOCKER_CONFIG it was given, that
directory's mode and the config file's mode and content) and answers the way the test
asks it to. The real binary is exercised by tests/e2e/test_registry.py and the kind tier.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.api.routers import harnesses
from crucible.adapters.execution import kubernetes
from crucible.adapters.execution.k8sfake import FakeKubernetesApi, FakeRegistry
from crucible.adapters.execution.k8sregistry import (
    CraneRegistryClient,
    RegistryAuth,
    RegistryError,
    auths_from_dockerconfigjson,
    parse_reference,
)
from crucible.adapters.execution.kubernetes import (
    LIST_IMAGES_CONCURRENCY,
    KubernetesConfig,
    KubernetesProvider,
)
from crucible.ports.execution import ImageInfo

DIGEST = "sha256:" + "a" * 64
CONFIG = {
    "config": {"Labels": {"crucible.harnesses": "codex", "crucible.harness.codex.version": "0.1"}}
}

STUB = """#!{python}
import json, os, stat, sys, time
directory = os.environ["DOCKER_CONFIG"]
path = os.path.join(directory, "config.json")
record = {{
    "argv": sys.argv[1:],
    "dir_mode": stat.S_IMODE(os.stat(directory).st_mode),
    "file_mode": stat.S_IMODE(os.stat(path).st_mode),
    "config": json.load(open(path)),
    "docker_config": directory,
    "pid": os.getpid(),
}}
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
mode = os.environ.get("STUB_MODE", "ok")
command = sys.argv[1]
if mode == "sleep" or (mode == "sleep-resolve" and command != "ls"):
    time.sleep(30)
if mode == "fail" or (mode == "fail-config" and command == "config"):
    sys.stderr.write(os.environ["STUB_STDERR"])
    sys.exit(1)
if command == "digest":
    print(os.environ.get("STUB_DIGEST", "{digest}"))
elif command == "config":
    sys.stdout.write(os.environ.get("STUB_CONFIG", {config!r}))
elif command == "ls":
    print("0.5.3")
    print("latest")
    print("")
"""


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "crane"
    binary.write_text(STUB.format(python=sys.executable, digest=DIGEST, config=json.dumps(CONFIG)))
    binary.chmod(0o755)
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("STUB_LOG", str(log))
    return binary


def _calls(stub: Path) -> list[dict[str, Any]]:
    log = stub.parent / "log.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


def _client(stub: Path, **kwargs: Any) -> CraneRegistryClient:
    return CraneRegistryClient(binary=str(stub), **kwargs)


# ----- argument shape -------------------------------------------------------


def test_resolve_reads_the_digest_then_the_amd64_config_by_that_digest(stub: Path) -> None:
    info = _client(stub).resolve("ghcr.io/sentania-labs/crucible-worker:0.5.3")

    assert [call["argv"] for call in _calls(stub)] == [
        ["digest", "ghcr.io/sentania-labs/crucible-worker:0.5.3"],
        [
            "config",
            "--platform",
            "linux/amd64",
            f"ghcr.io/sentania-labs/crucible-worker@{DIGEST}",
        ],
    ]
    assert info.digest == DIGEST
    assert info.reference == f"ghcr.io/sentania-labs/crucible-worker@{DIGEST}"
    assert info.harnesses == {"codex": "0.1"}


def test_a_docker_hub_short_name_is_named_in_full_and_pinned_without_the_host(
    stub: Path,
) -> None:
    info = _client(stub).resolve("busybox")

    assert _calls(stub)[0]["argv"] == ["digest", "docker.io/library/busybox:latest"]
    assert info.reference == f"library/busybox@{DIGEST}"


def test_a_reference_with_a_tag_and_a_digest_resolves_by_the_digest(stub: Path) -> None:
    _client(stub).resolve(f"ghcr.io/o/r:1.0@{DIGEST}")

    assert _calls(stub)[0]["argv"] == ["digest", f"ghcr.io/o/r@{DIGEST}"]


def test_list_tags_runs_ls_on_the_repository_and_drops_blank_lines(stub: Path) -> None:
    tags = _client(stub).list_tags("ghcr.io/sentania-labs/crucible-worker")

    assert tags == ["0.5.3", "latest"]
    assert _calls(stub)[0]["argv"] == ["ls", "ghcr.io/sentania-labs/crucible-worker"]


# ----- the credential directory -------------------------------------------


def test_the_credential_reaches_crane_only_through_a_private_docker_config(
    stub: Path,
) -> None:
    client = _client(
        stub,
        auths={
            "ghcr.io": RegistryAuth("robot", "hunter2-secret"),
            "quay.io": RegistryAuth("other", "not-for-ghcr"),
        },
    )
    client.resolve("ghcr.io/o/r:1")

    for call in _calls(stub):
        # The access bits. A directory made under a setgid parent inherits the setgid
        # bit, which is what a Pod's fsGroup volume does to /tmp (hades #184); it
        # decides the group new files get, and grants no one access.
        assert call["dir_mode"] & 0o777 == 0o700
        assert call["dir_mode"] & ~0o777 in (0, stat.S_ISGID)
        assert call["file_mode"] == 0o600
        # Only the registry being read; never the argv.
        assert call["config"] == {
            "auths": {"ghcr.io": {"auth": base64.b64encode(b"robot:hunter2-secret").decode()}}
        }
        assert not any("hunter2" in arg or "robot" in arg for arg in call["argv"])
        assert not os.path.exists(call["docker_config"])


def test_docker_hub_credentials_use_the_key_crane_looks_up(stub: Path) -> None:
    _client(stub, auths={"docker.io": RegistryAuth("u", "p")}).list_tags("library/busybox")

    assert list(_calls(stub)[0]["config"]["auths"]) == ["https://index.docker.io/v1/"]


def test_no_credential_still_gets_an_empty_private_config(stub: Path) -> None:
    _client(stub).list_tags("ghcr.io/o/r")

    call = _calls(stub)[0]
    assert call["config"] == {"auths": {}}
    assert call["docker_config"] != os.path.expanduser("~/.docker")


def test_the_credential_directory_is_removed_when_crane_fails(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "fail")
    monkeypatch.setenv("STUB_STDERR", "Error: GET https://ghcr.io/v2/o/r/manifests/x: DENIED\n")
    with pytest.raises(RegistryError):
        _client(stub, auths={"ghcr.io": RegistryAuth("u", "p")}).resolve("ghcr.io/o/r:x")

    assert not os.path.exists(_calls(stub)[0]["docker_config"])


def test_the_credential_directory_is_removed_when_crane_times_out(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "sleep")
    with pytest.raises(RegistryError):
        _client(stub, timeout=0.5).resolve("ghcr.io/o/r:x")

    assert not os.path.exists(_calls(stub)[0]["docker_config"])


def test_the_credential_directory_is_removed_when_crane_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    client = CraneRegistryClient(binary=str(tmp_path / "no-such-crane"))
    with pytest.raises(RegistryError, match="is not installed"):
        client.resolve("ghcr.io/o/r:1")

    assert list(tmp_path.iterdir()) == []


# ----- error mapping ------------------------------------------------------


def test_crane_error_line_is_the_reason_with_the_registry_named(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "fail")
    monkeypatch.setenv(
        "STUB_STDERR",
        "2026/09/24 22:12:58 HEAD request failed, falling back on GET\n"
        "Error: GET https://ghcr.io/v2/o/r/manifests/nope: MANIFEST_UNKNOWN: manifest unknown\n",
    )
    with pytest.raises(RegistryError) as caught:
        _client(stub).resolve("ghcr.io/o/r:nope")

    assert str(caught.value) == (
        "ghcr.io: GET https://ghcr.io/v2/o/r/manifests/nope: MANIFEST_UNKNOWN: manifest unknown"
    )


def test_a_credential_value_in_crane_output_is_redacted(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = RegistryAuth("robot", "hunter2-secret")
    monkeypatch.setenv("STUB_MODE", "fail")
    monkeypatch.setenv("STUB_STDERR", f"Error: hunter2-secret and {auth.encoded()}\n")
    with pytest.raises(RegistryError) as caught:
        _client(stub, auths={"ghcr.io": auth}).resolve("ghcr.io/o/r:1")

    assert "hunter2" not in str(caught.value)
    assert auth.encoded() not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_a_failed_config_read_is_a_registry_error(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "fail-config")
    monkeypatch.setenv(
        "STUB_STDERR", "Error: no child with platform linux/amd64 in index ghcr.io/o/r\n"
    )
    with pytest.raises(RegistryError, match="no child with platform linux/amd64"):
        _client(stub).resolve("ghcr.io/o/r:1")


def test_output_that_is_not_a_digest_is_refused(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_DIGEST", "<html>moved</html>")
    with pytest.raises(RegistryError, match="resolved to no digest"):
        _client(stub).resolve("ghcr.io/o/r:1")


def test_a_config_that_is_not_json_is_refused(stub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_CONFIG", "not json")
    with pytest.raises(RegistryError, match="not JSON"):
        _client(stub).resolve("ghcr.io/o/r:1")


def test_a_crane_call_is_bounded_by_the_timeout(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "sleep")
    started = time.monotonic()
    with pytest.raises(RegistryError, match=r"ghcr.io did not answer within 0.5s"):
        _client(stub, timeout=0.5).list_tags("ghcr.io/o/r")

    assert time.monotonic() - started < 10


def test_a_crane_call_ends_at_the_callers_deadline(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "sleep")
    started = time.monotonic()
    with pytest.raises(RegistryError, match="did not answer"):
        _client(stub, timeout=20).list_tags("ghcr.io/o/r", deadline=started + 0.5)

    assert time.monotonic() - started < 10
    # With the deadline already past, crane is not started at all.
    calls = len(_calls(stub))
    with pytest.raises(RegistryError, match="no time left"):
        _client(stub).resolve("ghcr.io/o/r:1", deadline=time.monotonic())
    assert len(_calls(stub)) == calls


# ----- the pull Secret ------------------------------------------------------


def test_pull_secret_entries_are_read_from_either_form() -> None:
    raw = json.dumps(
        {
            "auths": {
                "https://index.docker.io/v1/": {"auth": base64.b64encode(b"hub:pw").decode()},
                "ghcr.io": {"username": "robot", "password": "tok"},
            }
        }
    ).encode()

    assert auths_from_dockerconfigjson(raw) == {
        "docker.io": RegistryAuth("hub", "pw"),
        "ghcr.io": RegistryAuth("robot", "tok"),
    }


# ----- plain HTTP ---------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    [
        "10.1.2.3:5000/worker:1",
        "172.20.0.1/worker:1",
        "192.168.1.5:443/worker:1",
        "registry.localhost:5000/worker:1",
    ],
)
def test_a_registry_crane_would_read_over_plain_http_is_refused(stub: Path, reference: str) -> None:
    client = _client(stub, auths={reference.split("/", maxsplit=1)[0]: RegistryAuth("u", "p")})
    with pytest.raises(RegistryError, match="plain HTTP"):
        client.resolve(reference)

    # Refused before crane ran, so no credential was ever written for it.
    assert not (stub.parent / "log.jsonl").exists()


@pytest.mark.parametrize(
    "reference", ["localhost:5000/worker:1", "127.0.0.1:5000/worker:1", "[::1]:5000/worker:1"]
)
def test_loopback_is_read_because_it_never_leaves_the_host(stub: Path, reference: str) -> None:
    _client(stub).resolve(reference)

    assert _calls(stub)[0]["argv"][0] == "digest"


# ----- listing ------------------------------------------------------------


class _SlowRegistry(FakeRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    def resolve(self, reference: str, *, deadline: float | None = None) -> ImageInfo:
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            time.sleep(0.05)
            return super().resolve(reference)
        finally:
            with self.lock:
                self.in_flight -= 1


async def test_list_images_skips_ci_proof_tags_without_resolving_them() -> None:
    """111: a ci-* tag is a CI proof push, never a promotable image, and about two
    thirds of a worker repository's tags are one. Listing costs two crane calls per
    tag resolved, so leaving them out of the resolve step, not just the result, is
    the point."""
    resolved: list[str] = []

    class _CountingRegistry(FakeRegistry):
        def resolve(self, reference: str, *, deadline: float | None = None) -> ImageInfo:
            resolved.append(reference)
            return super().resolve(reference)

    registry = _CountingRegistry()
    registry.register("ghcr.io/o/worker:1.0.0")
    registry.register("ghcr.io/o/worker:latest")
    registry.register("ghcr.io/o/worker:ci-0123abcd-20260916-aaaaaaaaaaaa")
    provider = KubernetesProvider(
        KubernetesConfig(image_repositories=("ghcr.io/o/worker",)),
        FakeKubernetesApi(),  # type: ignore[arg-type]
        registry,
    )

    images = await provider.list_images()

    assert {i.reference for i in images} == {
        "ghcr.io/o/worker:1.0.0",
        "ghcr.io/o/worker:latest",
    }
    assert resolved == ["ghcr.io/o/worker:1.0.0", "ghcr.io/o/worker:latest"]


async def test_list_images_resolves_a_few_tags_at_once_and_skips_failures() -> None:
    registry = _SlowRegistry()
    for n in range(20):
        registry.register(f"ghcr.io/o/worker:t{n:02d}")
    registry.unavailable.add("ghcr.io/o/worker:t03")
    provider = KubernetesProvider(
        KubernetesConfig(image_repositories=("ghcr.io/o/worker",)),
        FakeKubernetesApi(),  # type: ignore[arg-type]
        registry,
    )

    images = await provider.list_images()

    assert [i.reference for i in images] == [
        f"ghcr.io/o/worker:t{n:02d}" for n in range(20) if n != 3
    ]
    assert 1 < registry.peak <= LIST_IMAGES_CONCURRENCY


async def test_a_tagged_repository_entry_is_skipped_when_its_references_fail_to_resolve() -> None:
    """80: `probe_image` mistakenly landing in `image_repositories` builds
    `<repo:tag>:<tag>` for every tag the registry lists under it. `parse_reference`
    does not reject that shape, so the skip is not a parse error in Crucible: resolving
    the reference fails (the real crane refuses it locally), and the entry is dropped
    the same way an unavailable one is."""
    parse_reference("ghcr.io/o/worker:probe:t00")  # does not raise: the skip is not here
    registry = FakeRegistry()
    registry.register("ghcr.io/o/worker:t00")
    registry._tags["ghcr.io/o/worker:probe"] = ["t00"]
    provider = KubernetesProvider(
        KubernetesConfig(image_repositories=("ghcr.io/o/worker", "ghcr.io/o/worker:probe")),
        FakeKubernetesApi(),  # type: ignore[arg-type]
        registry,
    )

    images = await provider.list_images()

    assert [i.reference for i in images] == ["ghcr.io/o/worker:t00"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _crane_provider(stub: Path) -> KubernetesProvider:
    return KubernetesProvider(
        KubernetesConfig(image_repositories=("ghcr.io/o/worker",)),
        FakeKubernetesApi(),  # type: ignore[arg-type]
        CraneRegistryClient(binary=str(stub)),
    )


def test_a_listing_is_bounded_below_the_endpoints_wait() -> None:
    assert kubernetes.LIST_IMAGES_DEADLINE < harnesses.LISTING_WAIT


async def test_a_slow_registry_ends_the_listing_and_its_crane_processes_first(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tags list, then every resolve hangs. The same ordering as the real constants, at
    # test scale: the listing's bound falls before the endpoint's wait.
    monkeypatch.setenv("STUB_MODE", "sleep-resolve")
    # Leave enough headroom for process startup on a loaded test worker. A one-second
    # bound could expire after `ls` and before either digest process was recorded,
    # which tested scheduler timing instead of the listing and cleanup contract.
    monkeypatch.setattr(kubernetes, "LIST_IMAGES_DEADLINE", 5.0)
    monkeypatch.setattr(harnesses, "LISTING_WAIT", 15.0)
    provider = _crane_provider(stub)

    started = time.monotonic()
    found = await harnesses._images(SimpleNamespace(providers=[provider]))  # type: ignore[arg-type]

    assert found == []
    assert time.monotonic() - started < 15.0
    calls = _calls(stub)
    assert [c["argv"][0] for c in calls].count("digest") == 2
    assert not [c["pid"] for c in calls if _alive(c["pid"])]
    assert not [c["docker_config"] for c in calls if Path(c["docker_config"]).exists()]


async def test_a_caller_that_gives_up_leaves_no_crane_process_past_the_bound(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "sleep-resolve")
    monkeypatch.setattr(kubernetes, "LIST_IMAGES_DEADLINE", 1.0)
    monkeypatch.setattr(harnesses, "LISTING_WAIT", 0.2)
    provider = _crane_provider(stub)

    started = time.monotonic()
    assert await harnesses._images(SimpleNamespace(providers=[provider])) == []  # type: ignore[arg-type]
    # The endpoint gave up; the shared listing goes on to its own bound and no further.
    listing = provider._listings[asyncio.get_running_loop()]
    await asyncio.wait_for(asyncio.shield(listing), timeout=5)

    assert time.monotonic() - started < 5.0
    calls = _calls(stub)
    assert calls
    assert not [c["pid"] for c in calls if _alive(c["pid"])]
    assert not [c["docker_config"] for c in calls if Path(c["docker_config"]).exists()]


class _StuckRegistry(FakeRegistry):
    """Every resolve blocks until the test releases it; the calls are counted."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.in_flight = 0
        self.listings = 0

    def list_tags(self, repository: str, *, deadline: float | None = None) -> list[str]:
        with self.lock:
            self.listings += 1
        return super().list_tags(repository)

    def resolve(self, reference: str, *, deadline: float | None = None) -> ImageInfo:
        with self.lock:
            self.in_flight += 1
        try:
            self.release.wait(10)
            return super().resolve(reference)
        finally:
            with self.lock:
                self.in_flight -= 1


async def test_stuck_registry_work_neither_starves_api_calls_nor_multiplies() -> None:
    # A default pool of two threads: had registry work run there, the API call below
    # would queue behind it.
    loop = asyncio.get_running_loop()
    default_pool = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(default_pool)
    registry = _StuckRegistry()
    for n in range(20):
        registry.register(f"ghcr.io/o/worker:t{n:02d}")
    provider = KubernetesProvider(
        KubernetesConfig(image_repositories=("ghcr.io/o/worker",)),
        FakeKubernetesApi(),  # type: ignore[arg-type]
        registry,
    )
    try:
        # A page polling the harnesses ten times while the registry is stuck.
        callers = [asyncio.create_task(provider.list_images()) for _ in range(10)]
        waited = time.monotonic() + 5
        while registry.in_flight < LIST_IMAGES_CONCURRENCY and time.monotonic() < waited:
            await asyncio.sleep(0.01)

        started = time.monotonic()
        assert await asyncio.wait_for(provider._call(lambda: "answered"), timeout=2) == "answered"
        assert time.monotonic() - started < 1.0
        assert registry.in_flight == LIST_IMAGES_CONCURRENCY
        assert registry.listings == 1

        registry.release.set()
        results = await asyncio.gather(*callers)
    finally:
        registry.release.set()
        default_pool.shutdown(wait=False)

    assert registry.listings == 1
    assert all(len(r) == 20 for r in results)
    assert all(r == results[0] for r in results)
