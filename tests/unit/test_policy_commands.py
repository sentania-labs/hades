"""The worker image must carry the program each shipped policy's checks start with (#181).

These tests cover the collection and the in-image probe without an image: the probe
script runs under a stand-in `docker` that executes it on the host with a PATH the test
controls. The proof against the real image is `make images-policy-check`, which CI runs
in the images job right after the build.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "images" / "policy_commands.py"
MIGRATIONS = REPOSITORY / "crucible" / "adapters" / "persistence" / "migrations" / "versions"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("policy_commands", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy_commands = _load()


def test_shipped_policies_include_the_seed_and_every_example() -> None:
    labels = [label for label, _ in policy_commands.shipped_policies()]
    assert labels[0].startswith("seeded default-software")
    examples = sorted((REPOSITORY / "examples" / "policies").glob("*.yaml"))
    assert examples, "no example policies found"
    for path in examples:
        assert str(path.relative_to(REPOSITORY)) in labels


SELF_HOSTING_PROGRAMS = {
    "git",
    "uv",
    "python3.12",
    "gitleaks",
    "bash",
    "jq",
    "tar",
    "sha256sum",
    "buildctl",
    "crane",
}


def test_the_shipped_policies_require_make() -> None:
    programs = policy_commands.required_programs(policy_commands.shipped_policies())
    assert set(programs) == {"make"} | SELF_HOSTING_PROGRAMS
    sources = "\n".join(programs["make"])
    for check in ("make lint", "make test", "make scan"):
        assert f"seeded default-software (migration 0001): {check}" in sources
        assert f"examples/policies/default-software.yaml: {check}" in sources
    for check in ("make lint", "make test-unit", "make scan"):
        assert f"examples/policies/hades-self-hosting.yaml: {check}" in sources


def test_declared_programs_are_probed_with_their_policy_named() -> None:
    """hades #184: what a policy's checks call beyond their first word, when it says so."""
    programs = policy_commands.required_programs(policy_commands.shipped_policies())
    for program in SELF_HOSTING_PROGRAMS:
        assert programs[program] == ["examples/policies/hades-self-hosting.yaml: required_programs"]
    assert policy_commands.required_programs(
        [("p", {"repository": {"required_checks": ["make lint"], "required_programs": ["uv"]}})]
    ) == {"make": ["p: make lint"], "uv": ["p: required_programs"]}


def test_only_migration_0001_seeds_required_checks() -> None:
    # The check reads the seed from migration 0001. A later migration that seeds its own
    # required checks would slip past it, so name it here and in shipped_policies().
    mentions = sorted(
        path.name for path in MIGRATIONS.glob("_*.py") if "required_checks" in path.read_text()
    )
    # 0007 only creates the ci_certifications.required_checks column (GitHub checks).
    assert mentions == ["_0001_walking_skeleton.py", "_0007_github_delivery.py"]
    lines = (MIGRATIONS / "_0007_github_delivery.py").read_text().splitlines()
    assert [line.strip() for line in lines if "required_checks" in line] == [
        'sa.Column("required_checks", JSONB, nullable=False),'
    ]


@pytest.mark.parametrize(
    ("check", "program"),
    [
        ("make lint", "make"),
        ("  make   scan ", "make"),
        ("CI=1 LANG=C make test", "make"),
        ("uv run pytest -q", "uv"),
        ("'./scripts/check all'", "./scripts/check all"),
    ],
)
def test_program_of(check: str, program: str) -> None:
    assert policy_commands.program_of(check) == program


def test_program_of_refuses_a_check_with_no_program() -> None:
    with pytest.raises(ValueError, match="names no program"):
        policy_commands.program_of("FOO=1")


def _fake_docker(tmp_path: Path, path_dir: Path) -> Path:
    """A `docker` that runs the probe on the host with PATH set to path_dir alone."""
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        '[ "$1" = run ] || exit 9\n'
        'while [ "$1" != "-c" ]; do shift; done\n'
        "shift\n"
        f'PATH="{path_dir}" exec /bin/sh -c "$@"\n'
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    return docker


def _executable(directory: Path, name: str, mode: int = 0o755) -> None:
    target = directory / name
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(mode)


def test_resolve_finds_executables_and_rejects_builtins_and_plain_files(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir, "make")
    _executable(bin_dir, "notexec", mode=0o644)
    docker = _fake_docker(tmp_path, bin_dir)
    found = policy_commands.resolve(
        [str(docker)], "crucible-worker:test", ["make", "notexec", "cd", "absent"]
    )
    assert found == {"make": str(bin_dir / "make"), "notexec": "", "cd": "", "absent": ""}


def _run_main(docker: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DOCKER"] = str(docker)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--image", "crucible-worker:test"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        cwd=REPOSITORY,
    )


def test_main_fails_when_rg_does_not_run_in_the_image(tmp_path: Path) -> None:
    """hades #385: Hermes's search tool needs ripgrep, which no policy names."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for program in {"make"} | SELF_HOSTING_PROGRAMS:
        _executable(bin_dir, program)
    result = _run_main(_fake_docker(tmp_path, bin_dir))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAILED   rg --version exited 127" in result.stderr
    assert "fails its self-test" in result.stderr


def test_main_fails_and_names_the_policy_when_make_is_missing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir, "rg")
    result = _run_main(_fake_docker(tmp_path, bin_dir))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "MISSING  make" in result.stderr
    assert "examples/policies/default-software.yaml: make lint" in result.stderr
    assert "lacks a program a shipped policy requires" in result.stderr


def test_main_fails_and_names_the_policy_when_a_declared_program_is_missing(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for program in {"make", "rg"} | SELF_HOSTING_PROGRAMS - {"gitleaks"}:
        _executable(bin_dir, program)
    result = _run_main(_fake_docker(tmp_path, bin_dir))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "MISSING  gitleaks" in result.stderr
    assert "examples/policies/hades-self-hosting.yaml: required_programs" in result.stderr


def test_main_passes_when_the_image_carries_every_program(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for program in {"make", "rg"} | SELF_HOSTING_PROGRAMS:
        _executable(bin_dir, program)
    result = _run_main(_fake_docker(tmp_path, bin_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok       rg --version" in result.stdout
    assert f"ok       make -> {bin_dir / 'make'}" in result.stdout
    assert f"ok       uv -> {bin_dir / 'uv'}" in result.stdout


def test_worker_image_reads_the_manifest() -> None:
    assert policy_commands.worker_image().startswith("crucible-worker:")
